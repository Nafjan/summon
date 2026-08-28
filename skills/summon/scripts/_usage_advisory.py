"""Pure, redacted usage summary for provider-inert decision explanations."""

from __future__ import annotations

from typing import Any


def project(*, imported: dict | None = None, live: dict | None = None) -> dict:
    observations: list[tuple[str, dict]] = []
    if imported is not None:
        if not isinstance(imported, dict) or not isinstance(imported.get("observations"), list):
            raise ValueError("imported usage status is invalid")
        observations.extend(("operator_export", item) for item in imported["observations"])
    if live is not None:
        if not isinstance(live, dict) or not isinstance(live.get("observations"), list):
            raise ValueError("live usage status is invalid")
        observations.extend(("authenticated_live", item) for item in live["observations"])
    if any(not isinstance(item, dict) for _source, item in observations):
        raise ValueError("usage observation is invalid")
    return {
        "state": "advisory_only",
        "reason": "exact_pin_preserved",
        "observations_considered": len(observations),
        "freshness": {
            "fresh": sum(item.get("freshness") == "fresh" for _source, item in observations),
            "stale": sum(item.get("freshness") == "stale" for _source, item in observations),
        },
        "dimensions": sorted({item.get("dimension") for _source, item in observations
                              if isinstance(item.get("dimension"), str)}),
        # The canonical decision schema intentionally keeps provider/account
        # detail out of this compact advisory. Full redacted observations stay
        # on `usage status`; routing remains exact-pin preserving here.
        "comparability": "unverified_semantics",
    }


__all__ = ["project"]
