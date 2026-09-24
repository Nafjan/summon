"""Authenticated source-family qualification for chat continuations.

Chat turns do not have background-job IDs.  This wrapper therefore binds a
qualification to the room/turn source-family identity instead of fabricating a
job record.  The parent stores the wrapper inside the launch guard; the child
can only present the exact bytes already committed by that parent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Mapping

from _launch_qualification import material_contract_for, revocation_id_for


SCHEMA = "summon.chat-launch-qualification/v1"
_DOMAIN = b"summon-chat-launch-qualification/v1:"
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset({
    "schema", "source_family_id", "turn_id", "operation", "backend", "transport",
    "registry_generation", "registry_digest", "adapter", "adapter_version",
    "external_cli_version", "material_contract", "executable_sha256",
    "launch_material_sha256", "observation", "status", "expires_at",
    "revocation_id", "auth",
})


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _auth(token: str, body: Mapping[str, object]) -> str:
    return hmac.new(token.encode("utf-8"), _DOMAIN + _canonical(body),
                    hashlib.sha256).hexdigest()


def issue(*, source_family_id: str, turn_id: str, backend: str, transport: str,
          registry_generation: int, registry_digest: str, adapter: str,
          adapter_version: str, external_cli_version: str,
          material_contract: str, executable_sha256: str,
          launch_material_sha256: str, expires_at: float,
          observation: Mapping[str, object] | None = None,
          revocation_id: str, token: str) -> dict[str, object]:
    # Qualifications bind the canonical launch observation projection, never
    # the richer in-memory record.  Keeping this normalization at the signer
    # boundary prevents callers from accidentally authenticating fields that
    # are not part of the launch contract.
    linked_observation = None
    if observation is not None:
        from _launch_binding import binding_projection
        linked_observation = binding_projection(observation)
        if linked_observation is None:
            raise ValueError("chat qualification observation is invalid")
    body = {
        "schema": SCHEMA, "source_family_id": source_family_id, "turn_id": turn_id,
        "operation": "chat_resume", "backend": backend, "transport": transport,
        "registry_generation": registry_generation, "registry_digest": registry_digest,
        "adapter": adapter, "adapter_version": adapter_version,
        "external_cli_version": external_cli_version,
        "material_contract": material_contract, "executable_sha256": executable_sha256,
        "launch_material_sha256": launch_material_sha256,
        "observation": linked_observation,
        "status": "qualified",
        "expires_at": expires_at, "revocation_id": revocation_id,
    }
    if not isinstance(token, str) or not token:
        raise ValueError("chat qualification token is unavailable")
    value = dict(body, auth=_auth(token, body))
    if not valid(value, token=token):
        raise ValueError("chat qualification fields are invalid")
    return value


def valid(value: object, *, token: str | None = None,
          observation: Mapping[str, object] | None = None,
          source_family_id: str | None = None, turn_id: str | None = None,
          now: float | None = None) -> bool:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        return False
    if value.get("schema") != SCHEMA or not _ID_RE.fullmatch(str(value.get("source_family_id", ""))):
        return False
    if not isinstance(value.get("turn_id"), str) or not value["turn_id"]:
        return False
    if source_family_id is not None and value.get("source_family_id") != source_family_id:
        return False
    if turn_id is not None and value.get("turn_id") != turn_id:
        return False
    for key in ("operation", "backend", "transport", "adapter", "adapter_version",
                "external_cli_version", "material_contract"):
        if not isinstance(value.get(key), str) or not value[key]:
            return False
    if value.get("operation") != "chat_resume":
        return False
    if value["external_cli_version"].lower() in {"not_declared", "unavailable", "unknown", "none"}:
        return False
    if not isinstance(value.get("registry_generation"), int) or value["registry_generation"] < 1:
        return False
    for key in ("executable_sha256", "launch_material_sha256", "revocation_id"):
        if not isinstance(value.get(key), str) or not _SHA_RE.fullmatch(value[key]):
            return False
    if value.get("revocation_id") != revocation_id_for(value):
        return False
    if material_contract_for(
            backend=value.get("backend"), transport=value.get("transport"),
            adapter=value.get("adapter"), adapter_version=value.get("adapter_version")) \
            != value.get("material_contract"):
        return False
    if value.get("status") != "qualified":
        return False
    observation_value = value.get("observation")
    if observation_value is not None:
        from _launch_binding import binding_projection, valid_projection
        if (not isinstance(observation_value, Mapping)
                or not valid_projection(observation_value)
                or binding_projection(observation_value) != dict(observation_value)):
            return False
    expires = value.get("expires_at")
    now = time.time() if now is None else now
    if (not isinstance(expires, (int, float)) or isinstance(expires, bool)
            or not math.isfinite(expires) or expires <= now):
        return False
    if token is not None:
        body = {key: item for key, item in value.items() if key != "auth"}
        if not hmac.compare_digest(str(value.get("auth", "")), _auth(token, body)):
            return False
    if observation is not None:
        for key in ("backend", "transport", "registry_generation", "registry_digest",
                    "adapter", "adapter_version", "external_cli_version",
                    "executable_sha256", "launch_material_sha256"):
            if observation.get(key) != value.get(key):
                return False
        linked = value.get("observation")
        if isinstance(linked, Mapping):
            from _launch_binding import binding_projection
            if binding_projection(observation) != dict(linked):
                return False
    return True


__all__ = ["SCHEMA", "issue", "valid"]
