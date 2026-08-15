"""Strict, provider-free access to Summon's editorial model catalog.

The catalog is presentation and recommendation metadata only.  It is never an
availability check and never authorizes a dispatch.  Exact service evidence is
carried separately by the dispatch envelope's ``model.served`` field.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


SCHEMA_VERSION = 1
_LABELS = frozenset({"frontier", "near-frontier"})
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/+()@-]{0,95}$")
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "references" / "model-catalog.json"


class ModelCatalogError(ValueError):
    """The editorial catalog is malformed or unsafe to display."""


@dataclass(frozen=True)
class ModelDisplay:
    role: str
    name: str
    version: str
    label: str
    lane: str
    availability: str = "catalog_listed"
    served_exact: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role, "name": self.name, "version": self.version,
            "label": self.label, "lane": self.lane,
            "availability": self.availability, "served_exact": self.served_exact,
        }


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
        raise ModelCatalogError(f"catalog {field} is invalid")
    return value


def load_catalog(path: str | Path = _CATALOG_PATH) -> dict[str, object]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelCatalogError("model catalog could not be read") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_VERSION:
        raise ModelCatalogError("model catalog schema is invalid")
    if raw.get("authority") != "summon-editorial" or not isinstance(raw.get("entries"), list):
        raise ModelCatalogError("model catalog authority or entries are invalid")
    keys: set[str] = set()
    seen_lanes: dict[str, set[int]] = {"frontier": set(), "near-frontier": set()}
    entries: list[dict[str, object]] = []
    for entry in raw["entries"]:
        if not isinstance(entry, dict):
            raise ModelCatalogError("catalog entry is invalid")
        key = _text(entry.get("key"), "key")
        if key in keys:
            raise ModelCatalogError("catalog keys must be unique")
        keys.add(key)
        identity = entry.get("identity"); display = entry.get("display")
        editorial = entry.get("editorial"); route = entry.get("route")
        if not all(isinstance(value, dict) for value in (identity, display, editorial, route)):
            raise ModelCatalogError(f"catalog entry {key} has invalid sections")
        vendor = _text(identity.get("vendor"), "vendor")
        model_id = _text(identity.get("model_id"), "model_id")
        label = editorial.get("label")
        rank = editorial.get("rank")
        lane = _text(editorial.get("lane"), "lane")
        if label not in _LABELS or isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ModelCatalogError(f"catalog entry {key} has invalid editorial ranking")
        if rank in seen_lanes[label]:
            raise ModelCatalogError(f"catalog {label} ranks must be unique")
        seen_lanes[label].add(rank)
        dispatchable = route.get("dispatchable")
        if not isinstance(dispatchable, bool):
            raise ModelCatalogError(f"catalog entry {key} route is invalid")
        backend = route.get("backend")
        target = route.get("target")
        if backend != "unverified" and (not isinstance(backend, str) or not backend):
            raise ModelCatalogError(f"catalog entry {key} backend is invalid")
        if target != model_id:
            raise ModelCatalogError(f"catalog entry {key} target must match model_id")
        entries.append({
            "key": key, "vendor": vendor, "model_id": model_id,
            "role": _text(display.get("role"), "role"),
            "name": _text(display.get("name"), "name"),
            "version": _text(display.get("version"), "version"),
            "label": label, "rank": rank, "lane": lane,
            "backend": backend, "target": target, "dispatchable": dispatchable,
        })
    raw["entries"] = entries
    return raw


def catalog_sha256(path: str | Path = _CATALOG_PATH) -> str:
    # Hash canonical parsed content so whitespace-only edits do not alter the
    # provenance shown to operators.
    canonical = json.dumps(load_catalog(path), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def display_for(backend: str | None, model_id: str | None, *,
                path: str | Path = _CATALOG_PATH) -> dict[str, object] | None:
    if not isinstance(backend, str) or not isinstance(model_id, str):
        return None
    catalog = load_catalog(path)
    for entry in catalog["entries"]:
        if entry["backend"] == backend and entry["target"] == model_id:
            item = ModelDisplay(
                # The model role is editorial catalog data.  Callers cannot
                # override it with a seat/persona role and accidentally make
                # the browser present an unverified identity.
                role=entry["role"],
                name=entry["name"], version=entry["version"], label=entry["label"],
                lane=entry["lane"],
            )
            value = item.as_dict()
            value["catalog_schema"] = SCHEMA_VERSION
            value["catalog_sha256"] = catalog_sha256(path)
            value["label_provenance"] = "summon-editorial"
            return value
    return None


def display_for_hash(backend: str | None, model_sha256: str | None, *,
                     path: str | Path = _CATALOG_PATH) -> dict[str, object] | None:
    """Resolve display metadata from a redacted plan model hash.

    Live receipts intentionally carry ``model_sha256`` rather than raw model
    strings.  Matching the catalog's exact target hash lets the local observer
    show a useful name/version without weakening that redaction boundary.
    """
    if not isinstance(backend, str) or not isinstance(model_sha256, str):
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", model_sha256):
        return None
    catalog = load_catalog(path)
    for entry in catalog["entries"]:
        if entry["backend"] != backend:
            continue
        expected = hashlib.sha256(str(entry["target"]).encode("utf-8")).hexdigest()
        if expected == model_sha256:
            return display_for(backend, str(entry["target"]), path=path)
    return None
