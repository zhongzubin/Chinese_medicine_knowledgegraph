# -*- coding: utf-8 -*-
"""API 配置和 API Key 鉴权测试。"""

import json

import pytest

from graph_qa.api_security import (
    APIConfigurationError,
    APIKeyAuthenticator,
    APISettings,
    AuthenticationError,
)


def make_settings(**overrides) -> APISettings:
    values = {
        "auth_required": True,
        "api_keys": {"client-a": "a" * 32},
        "redis_url": "redis://unit-test:6379/0",
    }
    values.update(overrides)
    return APISettings(**values)


def test_settings_load_multiple_keys_and_hide_values_from_repr(monkeypatch):
    keys = {"client-a": "a" * 32, "client-b": "b" * 32}
    monkeypatch.setenv("GRAPH_QA_REDIS_URL", "redis://unit-test:6379/0")
    monkeypatch.setenv("GRAPH_QA_API_KEYS_JSON", json.dumps(keys))

    settings = APISettings.from_environment()

    assert dict(settings.api_keys) == keys
    assert "a" * 32 not in repr(settings)
    assert "redis://unit-test" not in repr(settings)


def test_settings_require_key_when_auth_is_enabled():
    with pytest.raises(APIConfigurationError, match="至少配置一个 API Key"):
        make_settings(api_keys={})


def test_settings_reject_short_or_duplicate_keys():
    with pytest.raises(APIConfigurationError, match="至少需要"):
        make_settings(api_keys={"short": "too-short"})

    with pytest.raises(APIConfigurationError, match="不能使用相同"):
        make_settings(api_keys={"a": "a" * 32, "b": "a" * 32})


def test_authenticator_returns_client_id_and_rejects_unknown_key():
    authenticator = APIKeyAuthenticator(make_settings())

    assert authenticator.authenticate("a" * 32) == "client-a"
    with pytest.raises(AuthenticationError, match="缺失或无效"):
        authenticator.authenticate("b" * 32)
