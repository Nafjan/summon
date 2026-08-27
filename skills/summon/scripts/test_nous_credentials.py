"""Focused tests for the local Nous/Hermes credential bridge."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import _apibackend
import _nous_credentials
from _builder import opencode_env_override


def test_profile_parser_reads_only_nous_key(tmp_path):
    env_file = tmp_path / "nous.env"
    env_file.write_text(
        "NOUS_MODEL=stealth/ox-alpha\n"
        "export NOUS_API_KEY='profile-secret'\n"
        "OTHER_SECRET=ignore-me\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"SUMMON_NOUS_ENV": str(env_file)}, clear=False):
        key, source = _nous_credentials.resolve_nous_api_key()
    assert key == "profile-secret"
    assert source == "hermes_profile"


def test_nous_dry_run_credential_availability_is_profile_aware():
    with patch("_nous_credentials.resolve_nous_api_key",
               return_value=("profile-secret", "hermes_profile")):
        assert _apibackend.api_key_available(
            "NOUS_API_KEY", "https://inference-api.nousresearch.com/v1") is True
    with patch("_nous_credentials.resolve_nous_api_key", return_value=(None, None)):
        assert _apibackend.api_key_available(
            "NOUS_API_KEY", "https://inference-api.nousresearch.com/v1") is False


def test_nous_dispatch_uses_profile_without_leaking_secret():
    inv = SimpleNamespace(
        model="stealth/ox-alpha",
        base_url="https://inference-api.nousresearch.com/v1",
        api_key_env="NOUS_API_KEY",
        system_context="",
        prompt="probe",
        allow_payg=False,
    )
    response = {"status": "success", "result": "OK", "model_resolved": "stealth/ox-alpha"}
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOUS_API_KEY", None)
        with patch("_nous_credentials.resolve_nous_api_key",
                   return_value=("profile-secret", "hermes_profile")), \
             patch("_apibackend._do_request", return_value=response) as request:
            out = _apibackend.call(inv, 1000)
    assert request.call_args.args[4] == "profile-secret"
    assert "profile-secret" not in json.dumps(out)
    assert any("Hermes Nous profile" in w for w in out["warnings"])


def test_nous_profile_auth_rejection_is_actionable_and_nonretryable():
    inv = SimpleNamespace(
        model="stealth/ox-alpha",
        base_url="https://inference-api.nousresearch.com/v1",
        api_key_env="NOUS_API_KEY",
        system_context="",
        prompt="probe",
        allow_payg=False,
    )
    response = {
        "status": "error",
        "result": "",
        "error": "HTTP 403 from https://inference-api.nousresearch.com/v1: error code: 1010",
    }
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOUS_API_KEY", None)
        with patch("_nous_credentials.resolve_nous_api_key",
                   return_value=("profile-secret", "hermes_profile")), \
             patch("_apibackend._do_request", return_value=response):
            out = _apibackend.call(inv, 1000)
    assert out["error_kind"] == "authentication_failed"
    assert out["retryable"] is False
    assert out["remediation_code"] == "nous_portal_auth_required"
    assert any("hermes auth add nous" in w for w in out["warnings"])
    assert "profile-secret" not in json.dumps(out)


def test_direct_request_sets_provider_compatible_headers():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({
        "model": "stealth/ox-alpha",
        "choices": [{"message": {"content": "OK"}}],
    }).encode("utf-8")
    opener = MagicMock()
    opener.open.return_value = response
    with patch("_apibackend._opener", return_value=opener):
        out = _apibackend._do_request(
            "https://inference-api.nousresearch.com/v1",
            "stealth/ox-alpha", "", "Reply with exactly OK.",
            "profile-secret", 1000, "openai-compat")
    request = opener.open.call_args.args[0]
    assert request.headers["Accept"] == "application/json"
    assert request.headers["User-agent"] == "summon-openai-compatible/1"
    assert out["status"] == "success"


def test_opencode_nous_bridge_is_child_only_and_configured():
    with patch("_nous_credentials.resolve_nous_api_key",
               return_value=("profile-secret", "hermes_profile")), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("NOUS_API_KEY", None)
        os.environ.pop("OPENCODE_CONFIG_CONTENT", None)
        env = opencode_env_override("nous/current-model")
    assert env["NOUS_API_KEY"] == "profile-secret"
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    provider = config["provider"]["nous"]
    assert provider["options"]["baseURL"] == "https://inference-api.nousresearch.com/v1"
    assert provider["options"]["apiKey"] == "{env:NOUS_API_KEY}"
    assert provider["models"]["current-model"]["name"] == "current-model"
    assert os.environ.get("NOUS_API_KEY") != "profile-secret"


def test_opencode_nous_bridge_rejects_malformed_dynamic_model():
    with pytest.raises(ValueError, match="empty or malformed"):
        opencode_env_override("nous/")
    with pytest.raises(ValueError, match="empty or malformed"):
        opencode_env_override("nous/bad\nmodel")
    with pytest.raises(ValueError, match="empty or malformed"):
        opencode_env_override("nous/bad model")
    with pytest.raises(ValueError, match="empty or malformed"):
        opencode_env_override("nous/bad\x7fmodel")
