"""Pure, redacted usage summary for provider-inert decision explanations."""

from __future__ import annotations

import re
from typing import Any


_ACCOUNT_SCOPE_HMAC = re.compile(r"^[0-9a-f]{64}$")


def _validate_live_observation(item: dict) -> None:
    """Require the evidence needed before a cached live value is considered.

    ``live`` is normally produced by the authenticated private usage store.  A
    caller can also construct this compact projection directly, so do not let
    a status-shaped or forged observation become an ``authenticated_live``
    source merely by being placed in a list.
    """
    if (item.get("status") != "success"
            or item.get("execution_status") != "success"
            or item.get("provider_contacted") is not True):
        raise ValueError("live usage observation lacks successful contact evidence")
    scope = item.get("account_scope_hmac")
    if not isinstance(scope, str) or _ACCOUNT_SCOPE_HMAC.fullmatch(scope) is None:
        raise ValueError("live usage observation lacks account scope evidence")


def project(*, imported: dict | None = None, live: dict | None = None) -> dict:
    observations: list[tuple[str, dict]] = []
    if imported is not None:
        if not isinstance(imported, dict) or not isinstance(imported.get("observations"), list):
            raise ValueError("imported usage status is invalid")
        observations.extend(("operator_export", item) for item in imported["observations"])
    if live is not None:
        if not isinstance(live, dict) or not isinstance(live.get("observations"), list):
            raise ValueError("live usage status is invalid")
        for item in live["observations"]:
            if not isinstance(item, dict):
                raise ValueError("usage observation is invalid")
            _validate_live_observation(item)
            observations.append(("authenticated_live", item))
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
