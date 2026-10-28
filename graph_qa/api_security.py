# -*- coding: utf-8 -*-
"""FastAPI 配置和静态 API Key 鉴权。"""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, Mapping, Optional

API_KEY_MIN_LENGTH = 32
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_REDIS_PREFIX_RE = re.compile(r"^[A-Za-z0-9:_-]+$")


class APIConfigurationError(ValueError):
    """API 启动配置缺失或非法。"""


class AuthenticationError(ValueError):
    """API Key 缺失或无效。"""


def _read_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise APIConfigurationError(f"{name} 必须是整数") from exc
    if value < 1:
        raise APIConfigurationError(f"{name} 必须大于等于 1")
    return value


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise APIConfigurationError(f"{name} 必须是 true 或 false")


def _read_api_keys() -> Dict[str, str]:
    raw_json = os.getenv("GRAPH_QA_API_KEYS_JSON")
    if raw_json and raw_json.strip():
        try:
            value = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise APIConfigurationError("GRAPH_QA_API_KEYS_JSON 必须是 JSON 对象") from exc
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise APIConfigurationError("GRAPH_QA_API_KEYS_JSON 必须映射客户端名到 Key")
        return value

    single_key = os.getenv("GRAPH_QA_API_KEY")
    if single_key and single_key.strip():
        return {"default": single_key}
    return {}


@dataclass(frozen=True)
class APISettings:
    auth_required: bool = True
    api_keys: Mapping[str, str] = field(default_factory=dict, repr=False)
    redis_url: str = field(default="", repr=False)
    redis_prefix: str = "graph_qa"
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60
    max_concurrent_requests: int = 4
    concurrency_lease_seconds: int = 60
    max_results: int = 50
    database: Optional[str] = None
    session_ttl_seconds: int = 86400
    session_max_turns: int = 5
    session_lock_seconds: int = 180

    def __post_init__(self) -> None:
        if not self.redis_url:
            raise APIConfigurationError("必须配置 GRAPH_QA_REDIS_URL")
        if not _REDIS_PREFIX_RE.fullmatch(self.redis_prefix):
            raise APIConfigurationError(
                "GRAPH_QA_REDIS_PREFIX 只能包含字母、数字、冒号、下划线或连字符"
            )
        if self.rate_limit_requests < 1:
            raise APIConfigurationError("限流请求数必须大于等于 1")
        if self.rate_limit_window_seconds < 1:
            raise APIConfigurationError("限流窗口必须大于等于 1 秒")
        if self.max_concurrent_requests < 1:
            raise APIConfigurationError("最大并发请求数必须大于等于 1")
        if self.concurrency_lease_seconds < 1:
            raise APIConfigurationError("并发租约时间必须大于等于 1 秒")
        if self.max_results < 1:
            raise APIConfigurationError("最大查询结果数必须大于等于 1")
        if self.session_ttl_seconds < 1:
            raise APIConfigurationError("会话 TTL 必须大于等于 1 秒")
        if self.session_max_turns < 1:
            raise APIConfigurationError("会话历史轮数必须大于等于 1")
        if self.session_lock_seconds < 1:
            raise APIConfigurationError("会话锁时间必须大于等于 1 秒")

        normalized_keys: Dict[str, str] = {}
        for client_id, api_key in self.api_keys.items():
            if not isinstance(client_id, str) or not _CLIENT_ID_RE.fullmatch(client_id):
                raise APIConfigurationError("API Key 客户端名格式非法")
            if (
                not isinstance(api_key, str)
                or api_key != api_key.strip()
                or len(api_key) < API_KEY_MIN_LENGTH
            ):
                raise APIConfigurationError(
                    f"客户端 {client_id} 的 API Key 至少需要 {API_KEY_MIN_LENGTH} 个字符"
                )
            normalized_keys[client_id] = api_key
        if len(set(normalized_keys.values())) != len(normalized_keys):
            raise APIConfigurationError("不同客户端不能使用相同 API Key")
        if self.auth_required and not normalized_keys:
            raise APIConfigurationError("启用鉴权时必须至少配置一个 API Key")
        object.__setattr__(self, "api_keys", MappingProxyType(normalized_keys))

    @classmethod
    def from_environment(cls) -> "APISettings":
        redis_url = (os.getenv("GRAPH_QA_REDIS_URL") or "").strip()
        if not redis_url:
            raise APIConfigurationError("缺少 GRAPH_QA_REDIS_URL")
        return cls(
            auth_required=_read_bool("GRAPH_QA_AUTH_REQUIRED", True),
            api_keys=_read_api_keys(),
            redis_url=redis_url,
            redis_prefix=(os.getenv("GRAPH_QA_REDIS_PREFIX") or "graph_qa").strip(),
            rate_limit_requests=_read_positive_int("GRAPH_QA_RATE_LIMIT_REQUESTS", 10),
            rate_limit_window_seconds=_read_positive_int(
                "GRAPH_QA_RATE_LIMIT_WINDOW_SECONDS",
                60,
            ),
            max_concurrent_requests=_read_positive_int(
                "GRAPH_QA_MAX_CONCURRENT_REQUESTS",
                4,
            ),
            concurrency_lease_seconds=_read_positive_int(
                "GRAPH_QA_CONCURRENCY_LEASE_SECONDS",
                60,
            ),
            max_results=_read_positive_int("GRAPH_QA_MAX_RESULTS", 50),
            database=(os.getenv("GRAPH_QA_DATABASE") or "").strip() or None,
            session_ttl_seconds=_read_positive_int(
                "GRAPH_QA_SESSION_TTL_SECONDS",
                86400,
            ),
            session_max_turns=_read_positive_int("GRAPH_QA_SESSION_MAX_TURNS", 5),
            session_lock_seconds=_read_positive_int(
                "GRAPH_QA_SESSION_LOCK_SECONDS",
                180,
            ),
        )


class APIKeyAuthenticator:
    """使用预计算摘要校验静态 API Key，不保存明文比较结果。"""

    def __init__(self, settings: APISettings):
        self.auth_required = settings.auth_required
        self._key_digests = tuple(
            (client_id, hashlib.sha256(api_key.encode("utf-8")).digest())
            for client_id, api_key in settings.api_keys.items()
        )

    def authenticate(self, api_key: Optional[str]) -> str:
        if not self.auth_required:
            return "anonymous"
        if not api_key:
            raise AuthenticationError("API Key 缺失或无效")

        submitted = hashlib.sha256(api_key.encode("utf-8")).digest()
        for client_id, expected in self._key_digests:
            if hmac.compare_digest(submitted, expected):
                return client_id
        raise AuthenticationError("API Key 缺失或无效")
