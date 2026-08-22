"""Focused tests for the local Windows Credential Manager fallback."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import _apibackend
import _windows_credentials


def test_decode_cmdkey_utf16_and_utf8():
    assert _windows_credentials._decode_blob("secret".encode("utf-16-le")) == "secret"
    assert _windows_credentials._decode_blob(b"secret") == "secret"
    assert _windows_credentials._decode_blob(b"\x00\x00") is None


def test_api_key_available_prefers_environment():
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-secret"}, clear=False):
        with patch("_windows_credentials.resolve_openrouter_api_key") as resolve:
            assert _apibackend.api_key_available(
                "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1") is True
            resolve.assert_not_called()

def test_api_key_available_checks_openrouter_store():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch("_windows_credentials.resolve_openrouter_api_key",
                   return_value=("store-secret", "windows_credential")):
            assert _apibackend.api_key_available(
                "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1") is True


def test_openrouter_dispatch_uses_store_without_leaking_secret():
    inv = SimpleNamespace(
        model="stealth/ox-alpha",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        system_context="",
        prompt="probe",
        allow_payg=False,
    )
    response = {"status": "success", "result": "ok", "model_resolved": "stealth/ox-alpha"}
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch("_windows_credentials.resolve_openrouter_api_key",
                   return_value=("store-secret", "windows_credential")), \
             patch("_apibackend._do_request", return_value=response) as request:
            out = _apibackend.call(inv, 1000)
    assert request.call_args.args[4] == "store-secret"
    assert "store-secret" not in str(out)
    assert any("Windows credential store" in w for w in out["warnings"])


def test_store_fallback_is_not_used_for_another_origin():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENROUTER_API_KEY", None)
        with patch("_windows_credentials.resolve_openrouter_api_key") as resolve:
            assert _apibackend.api_key_available(
                "OPENROUTER_API_KEY", "https://example.invalid/v1") is False
            resolve.assert_not_called()
