"""Provider-inert usage evidence cache for Summon.

This module deliberately does not query providers.  It validates a small,
redacted interchange format and stores only normalized observations.  Live
provider adapters belong to a later, separately authorized slice.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
import os
import re
import tempfile
from pathlib import Path


SCHEMA = "summon.usage/v1"
MAX_BYTES = 256 * 1024
MAX_OBSERVATIONS = 128
MAX_TTL_SECONDS = 31 * 24 * 60 * 60
MAX_COUNT_VALUE = (1 << 63) - 1
MAX_CREDIT_VALUE = 1_000_000_000_000
MIN_TIMESTAMP_YEAR = 2000
MAX_TIMESTAMP_YEAR = 2100
MAX_CLOCK_SKEW_SECONDS = 5 * 60
SUPPORT_STATES = {"supported", "unsupported", "unavailable", "unknown"}
SOURCES = {"operator_export", "local_cli", "provider_endpoint", "cache"}
DIMENSIONS = {
    "subscription_allowance", "api_balance", "account_credit", "rate_limit", "unknown"
}
UNITS_BY_DIMENSION = {
    "subscription_allowance": {"fraction", "percent", "tokens", "requests"},
    "api_balance": {"usd", "credit_units"},
    "account_credit": {"usd", "credit_units"},
    "rate_limit": {"fraction", "percent", "tokens", "requests"},
    "unknown": {"unknown"},
}
PROVIDERS = {
    "agy", "anthropic", "arkcli", "byteplus", "claude", "codex", "cursor",
    "gemini", "kimi", "nous", "openai", "opencode", "openrouter",
}
CAPABILITIES = {
    provider: {
        "provider": provider,
        "live_refresh": "deferred",
        "support": "unknown",
        "provider_contacted": False,
    }
    for provider in sorted(PROVIDERS)
}
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_OBSERVATION_FIELDS = {
    "provider", "dimension", "support", "source", "observed_at", "retrieved_at",
    "ttl_seconds", "latency_ms", "remaining", "reset_at",
}
_REMAINING_FIELDS = {"value", "unit"}


def _utc_now() -> _datetime.datetime:
    return _datetime.datetime.now(_datetime.timezone.utc)


def _parse_time(value: str, field: str) -> _datetime.datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    parsed = parsed.astimezone(_datetime.timezone.utc)
    if not MIN_TIMESTAMP_YEAR <= parsed.year <= MAX_TIMESTAMP_YEAR:
        raise ValueError(
            f"{field} year must be between {MIN_TIMESTAMP_YEAR} and {MAX_TIMESTAMP_YEAR}")
    return parsed


def _format_time(value: _datetime.datetime) -> str:
    return value.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _now(value: str | None) -> _datetime.datetime:
    return _parse_time(value, "now") if value is not None else _utc_now()


def _cache_path(cache_path: str | None = None) -> Path:
    selected = cache_path or os.environ.get("SUMMON_USAGE_CACHE")
    if selected:
        return Path(selected).expanduser()
    return Path.home() / ".agents" / "summon-usage-v1.json"


def _read_json(path: Path) -> dict:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("usage snapshot is not readable") from exc
    if size > MAX_BYTES:
        raise ValueError(f"usage snapshot exceeds {MAX_BYTES} bytes")
    def no_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = item
        return result

    def no_nonfinite(token):
        raise ValueError(f"non-finite JSON number: {token}")

    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"),
                           object_pairs_hook=no_duplicates,
                           parse_constant=no_nonfinite)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("usage snapshot must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("usage snapshot must be a JSON object")
    return value


def _bounded_number(value: object, field: str, *, minimum: float = 0,
                    maximum: float | None = None,
                    integer_only: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if integer_only and not isinstance(value, int):
        raise ValueError(f"{field} must be an integer for this unit")
    if integer_only:
        # Compare integers as integers. Converting 64-bit counts to float first
        # rounds adjacent valid boundary values and can reject MAX-1 as MAX+1.
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{field} is outside the supported range")
        return value
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} is outside the supported range") from exc
    if (not math.isfinite(number) or number < minimum
            or (maximum is not None and number > maximum)):
        suffix = f"..{maximum:g}" if maximum is not None else " or greater"
        raise ValueError(f"{field} must be {minimum:g}{suffix}")
    return number


def _bounded_integer(value: object, field: str, *, minimum: int = 0,
                     maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{field} is outside the supported range")
    return value


def _validate_observation(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("each usage observation must be an object")
    unsupported = sorted(set(value) - _OBSERVATION_FIELDS)
    if unsupported:
        raise ValueError("unsupported observation field: " + ", ".join(unsupported))
    missing = sorted({"provider", "dimension", "support", "source", "observed_at",
                      "retrieved_at", "ttl_seconds"} - set(value))
    if missing:
        raise ValueError("missing observation field: " + ", ".join(missing))

    provider = value["provider"]
    if (not isinstance(provider, str) or not _PROVIDER_RE.fullmatch(provider)
            or provider not in PROVIDERS):
        raise ValueError("provider must be a supported public provider identifier")
    dimension = value["dimension"]
    support = value["support"]
    source = value["source"]
    if not isinstance(dimension, str) or dimension not in DIMENSIONS:
        raise ValueError("unsupported usage dimension")
    if not isinstance(support, str) or support not in SUPPORT_STATES:
        raise ValueError("unsupported usage support state")
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("unsupported usage observation source")

    observed = _parse_time(value["observed_at"], "observed_at")
    retrieved = _parse_time(value["retrieved_at"], "retrieved_at")
    if retrieved < observed:
        raise ValueError("retrieved_at cannot precede observed_at")
    ttl = _bounded_integer(value["ttl_seconds"], "ttl_seconds", minimum=1,
                           maximum=MAX_TTL_SECONDS)
    latency = _bounded_integer(value.get("latency_ms", 0), "latency_ms",
                               maximum=24 * 60 * 60 * 1000)

    result = {
        "provider": provider,
        "dimension": dimension,
        "support": support,
        "source": source,
        "observed_at": _format_time(observed),
        "retrieved_at": _format_time(retrieved),
        "ttl_seconds": ttl,
        "latency_ms": latency,
    }
    remaining = value.get("remaining")
    if remaining is not None:
        if support != "supported" or dimension == "unknown":
            raise ValueError(
                "remaining requires support=supported and a known usage dimension")
        if not isinstance(remaining, dict):
            raise ValueError("remaining must be an object")
        unsupported_remaining = sorted(set(remaining) - _REMAINING_FIELDS)
        if unsupported_remaining:
            raise ValueError("unsupported remaining field: " +
                             ", ".join(unsupported_remaining))
        if set(remaining) != _REMAINING_FIELDS:
            raise ValueError("remaining requires value and unit")
        unit = remaining["unit"]
        if not isinstance(unit, str) or unit not in UNITS_BY_DIMENSION[dimension]:
            raise ValueError("remaining unit is incompatible with usage dimension")
        maximum = (
            1 if unit == "fraction" else
            100 if unit == "percent" else
            MAX_COUNT_VALUE if unit in {"tokens", "requests"} else
            MAX_CREDIT_VALUE
        )
        amount = _bounded_number(
            remaining["value"], "remaining.value", maximum=maximum,
            integer_only=unit in {"tokens", "requests"})
        result["remaining"] = {"value": amount, "unit": unit}
    if value.get("reset_at") is not None:
        reset = _parse_time(value["reset_at"], "reset_at")
        if reset < observed:
            raise ValueError("reset_at cannot precede observed_at")
        result["reset_at"] = _format_time(reset)
    return result


def _validate_snapshot(value: dict, *, allow_cached_at: bool = False) -> dict:
    allowed = {"schema", "observations"} | ({"cached_at"} if allow_cached_at else set())
    unsupported = sorted(set(value) - allowed)
    if unsupported:
        raise ValueError("unsupported usage snapshot field: " + ", ".join(unsupported))
    if value.get("schema") != SCHEMA:
        raise ValueError(f"usage snapshot schema must be {SCHEMA}")
    observations = value.get("observations")
    if not isinstance(observations, list):
        raise ValueError("usage observations must be an array")
    if len(observations) > MAX_OBSERVATIONS:
        raise ValueError(f"usage snapshot exceeds {MAX_OBSERVATIONS} observations")
    normalized = [_validate_observation(item) for item in observations]
    if allow_cached_at and value.get("cached_at") is not None:
        cached_at = _format_time(_parse_time(value["cached_at"], "cached_at"))
    else:
        cached_at = None
    result = {"schema": SCHEMA, "observations": normalized}
    if cached_at:
        result["cached_at"] = cached_at
    return result


def _validate_observation_clock(observations: list[dict], reference: _datetime.datetime) -> None:
    latest = reference + _datetime.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
    for item in observations:
        if (_parse_time(item["observed_at"], "observed_at") > latest
                or _parse_time(item["retrieved_at"], "retrieved_at") > latest):
            raise ValueError(
                f"usage observation is too far in the future (maximum clock skew "
                f"{MAX_CLOCK_SKEW_SECONDS} seconds)")


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"normalized usage cache exceeds {MAX_BYTES} bytes")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError("usage cache could not be replaced atomically") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def import_snapshot(source_path: str, *, cache_path: str | None = None,
                    now: str | None = None) -> dict:
    """Validate and cache an operator-exported redacted snapshot."""
    source = Path(source_path)
    normalized = _validate_snapshot(_read_json(source))
    # An unsigned local import cannot promote itself into provider-attested
    # evidence merely by spelling `provider_endpoint` or `local_cli` in JSON.
    # Future adapters may mint those sources after executing their own bounded
    # command; this interface records only what it can prove: operator export.
    if any(item["source"] != "operator_export" for item in normalized["observations"]):
        raise ValueError("usage import accepts only source=operator_export")
    imported_at = _now(now)
    _validate_observation_clock(normalized["observations"], imported_at)
    normalized["cached_at"] = _format_time(imported_at)
    _atomic_write(_cache_path(cache_path), normalized)
    return {
        "schema": SCHEMA,
        "status": "success",
        "provider_contacted": False,
        "imported": len(normalized["observations"]),
        "cache_state": "current",
    }


def _with_freshness(observation: dict, now: _datetime.datetime) -> dict:
    result = dict(observation)
    retrieved = _parse_time(result["retrieved_at"], "retrieved_at")
    expires = retrieved + _datetime.timedelta(seconds=result["ttl_seconds"])
    result["freshness"] = "fresh" if now <= expires else "stale"
    result["expires_at"] = _format_time(expires)
    return result


def status(*, cache_path: str | None = None, now: str | None = None) -> dict:
    """Return the local normalized cache without contacting a provider."""
    path = _cache_path(cache_path)
    if not path.exists():
        return {
            "schema": SCHEMA,
            "status": "success",
            "provider_contacted": False,
            "cache_state": "missing",
            "observations": [],
            "capabilities": _capabilities(),
            "selection_advice": _selection_advice([]),
        }
    checked_at = _now(now)
    snapshot = _validate_snapshot(_read_json(path), allow_cached_at=True)
    if "cached_at" not in snapshot:
        raise ValueError("usage cache is missing cached_at")
    cached_at = _parse_time(snapshot["cached_at"], "cached_at")
    if cached_at > checked_at + _datetime.timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        raise ValueError(
            f"usage cache timestamp is too far in the future (maximum clock skew "
            f"{MAX_CLOCK_SKEW_SECONDS} seconds)")
    if any(item["source"] != "operator_export" for item in snapshot["observations"]):
        raise ValueError("usage cache accepts only source=operator_export in this release")
    # Do not compose two independent skew allowances. An editable cache may be
    # up to five minutes ahead, but its observations are still bounded by the
    # earlier of cache time and this read's clock.
    _validate_observation_clock(snapshot["observations"], min(cached_at, checked_at))
    observations = [_with_freshness(item, checked_at)
                    for item in snapshot["observations"]]
    cache_state = "stale" if observations and all(
        item["freshness"] == "stale" for item in observations) else "current"
    return {
        "schema": SCHEMA,
        "status": "success",
        "provider_contacted": False,
        "cache_state": cache_state,
        "observations": observations,
        "capabilities": _capabilities(),
        "selection_advice": _selection_advice(observations),
    }


def _portable_observation(value: dict) -> dict:
    """Drop local-only fields and downgrade evidence for portable export.

    A copied snapshot cannot carry the authenticated private store or provider
    attestation with it.  Export therefore becomes an explicit operator export;
    consumers must not promote it back to live/provider-reported evidence.
    """
    allowed = {
        "provider", "dimension", "support", "retrieved_at", "remaining", "reset_at",
    }
    item = {key: value[key] for key in allowed if key in value}
    retrieved = item.get("retrieved_at")
    if not isinstance(retrieved, str):
        raise ValueError("usage export observation is missing retrieved_at")
    expires = value.get("expires_at")
    ttl = DEFAULT_EXPORT_TTL_SECONDS
    if isinstance(expires, str):
        seconds = int((_parse_time(expires, "expires_at")
                       - _parse_time(retrieved, "retrieved_at")).total_seconds())
        if 1 <= seconds <= MAX_TTL_SECONDS:
            ttl = seconds
    item.update({
        "source": "operator_export",
        "observed_at": retrieved,
        "ttl_seconds": ttl,
        "latency_ms": 0,
    })
    return _validate_observation(item)


DEFAULT_EXPORT_TTL_SECONDS = 300


def _capabilities() -> list[dict]:
    """Merge the provider-inert import registry with reviewed live adapters."""
    import _usage_live

    live = {item["provider"]: item for item in _usage_live.capabilities()}
    result = []
    for provider in sorted(PROVIDERS):
        item = dict(CAPABILITIES[provider])
        adapter = live.get(provider)
        if adapter is not None:
            state = adapter["state"]
            item.update(adapter)
            item["live_refresh"] = state
            item["support"] = (
                "supported" if state == "fixture_supported"
                else "unsupported" if state == "unsupported" else "unknown")
            item["provider_contacted"] = False
        result.append(item)
    return result


def _write_new(path: Path, value: dict) -> None:
    """Write a portable artifact once; never replace an existing or linked target."""
    if path.exists() or path.is_symlink():
        raise ValueError("usage output already exists")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("usage output directory could not be prepared") from exc
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")) + "\n"
    raw = payload.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"usage output exceeds {MAX_BYTES} bytes")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValueError("usage output already exists") from exc
    except OSError as exc:
        raise ValueError("usage output could not be written") from exc


def export_snapshot(output_path: str, *, cache_path: str | None = None,
                    live_status: dict | None = None, now: str | None = None) -> dict:
    """Write a redacted, de-attested portable snapshot without provider contact."""
    observations = []
    imported = status(cache_path=cache_path, now=now)
    observations.extend(_portable_observation(item)
                        for item in imported.get("observations", []))
    if live_status is not None:
        if (not isinstance(live_status, dict)
                or live_status.get("status") != "success"
                or not isinstance(live_status.get("observations"), list)):
            raise ValueError("live usage status is invalid")
        observations.extend(_portable_observation(item)
                            for item in live_status["observations"])
    normalized = _validate_snapshot({"schema": SCHEMA, "observations": observations})
    _write_new(Path(output_path), normalized)
    return {
        "schema": SCHEMA, "status": "success", "provider_contacted": False,
        "exported": len(observations), "portable_attestation": "operator_export",
    }


def synthetic_snapshot() -> dict:
    """Deterministic, non-account example spanning every advisory dimension/state."""
    base = "2026-01-01T00:00:00Z"
    values = [
        ("codex", "subscription_allowance", "supported", {"value": 75.0, "unit": "percent"}),
        ("openrouter", "api_balance", "supported", {"value": 12.5, "unit": "usd"}),
        ("arkcli", "account_credit", "supported", {"value": 1000.0, "unit": "credit_units"}),
        ("agy", "rate_limit", "supported", {"value": 50.0, "unit": "percent"}),
        ("claude", "unknown", "unsupported", None),
        ("kimi", "unknown", "unknown", None),
    ]
    observations = []
    for provider, dimension, support, remaining in values:
        item = {
            "provider": provider, "dimension": dimension, "support": support,
            "source": "operator_export", "observed_at": base,
            "retrieved_at": base, "ttl_seconds": DEFAULT_EXPORT_TTL_SECONDS,
            "latency_ms": 0,
        }
        if remaining is not None:
            item["remaining"] = remaining
        observations.append(_validate_observation(item))
    return {"schema": SCHEMA, "observations": observations}


def write_synthetic_snapshot(output_path: str) -> dict:
    value = synthetic_snapshot()
    _write_new(Path(output_path), value)
    return {
        "schema": SCHEMA, "status": "success", "provider_contacted": False,
        "example": True, "observations": len(value["observations"]),
    }


def _selection_advice(observations: list[dict]) -> dict:
    fresh = sum(item.get("freshness") == "fresh" for item in observations)
    stale = sum(item.get("freshness") == "stale" for item in observations)
    return {
        "routing_changed": False,
        "provider_contacted": False,
        "reason": "advisory_only_exact_requests_preserved",
        "fresh_observations": fresh,
        "stale_observations": stale,
    }


def compare_observations(left: dict, right: dict) -> dict:
    """State whether two normalized values have common semantics.

    This helper intentionally does not rank values.  It exists so callers cannot
    accidentally compare unlike categories merely because both contain numbers.
    """
    left_remaining = left.get("remaining")
    right_remaining = right.get("remaining")
    if not isinstance(left_remaining, dict) or not isinstance(right_remaining, dict):
        return {"comparable": False, "reason": "value_unavailable"}
    if (left.get("support") != "supported" or right.get("support") != "supported"
            or left.get("dimension") == "unknown" or right.get("dimension") == "unknown"):
        return {"comparable": False, "reason": "value_unavailable"}
    same_category = (
        left.get("provider") == right.get("provider")
        and left.get("dimension") == right.get("dimension")
        and left_remaining.get("unit") == right_remaining.get("unit")
    )
    if not same_category:
        return {"comparable": False, "reason": "category_incomparable"}
    if left.get("source") == "operator_export" or right.get("source") == "operator_export":
        return {"comparable": False, "reason": "unverified_semantics"}
    if left.get("freshness") == "stale" or right.get("freshness") == "stale":
        return {"comparable": False, "reason": "stale"}
    return {"comparable": True, "reason": "same_provider_dimension_unit"}
