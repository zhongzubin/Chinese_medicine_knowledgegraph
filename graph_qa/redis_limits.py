# -*- coding: utf-8 -*-
"""跨 worker 和实例共享的 Redis 限流与并发租约。"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import redis

from graph_qa.api_security import APISettings

logger = logging.getLogger(__name__)

RATE_LIMIT_SCRIPT = """
local current = redis.call("TIME")
local now = tonumber(current[1]) * 1000 + math.floor(tonumber(current[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local window_start = now - window

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", window_start)
local count = redis.call("ZCARD", KEYS[1])
if count >= limit then
    local oldest = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
    local retry_after = math.max(
        1,
        math.ceil((tonumber(oldest[2]) + window - now) / 1000)
    )
    return {0, 0, retry_after, retry_after}
end

redis.call("ZADD", KEYS[1], now, ARGV[3])
redis.call("PEXPIRE", KEYS[1], window + 1000)
local oldest = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
local reset_after = math.max(
    1,
    math.ceil((tonumber(oldest[2]) + window - now) / 1000)
)
return {1, limit - count - 1, 0, reset_after}
"""

ACQUIRE_CONCURRENCY_SCRIPT = """
local current = redis.call("TIME")
local now = tonumber(current[1]) * 1000 + math.floor(tonumber(current[2]) / 1000)
local lease = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])

redis.call("ZREMRANGEBYSCORE", KEYS[1], "-inf", now)
local count = redis.call("ZCARD", KEYS[1])
if count >= limit then
    return {0, 0}
end

redis.call("ZADD", KEYS[1], now + lease, ARGV[3])
redis.call("PEXPIRE", KEYS[1], lease + 1000)
return {1, count + 1}
"""

RENEW_CONCURRENCY_SCRIPT = """
local current = redis.call("TIME")
local now = tonumber(current[1]) * 1000 + math.floor(tonumber(current[2]) / 1000)
local lease = tonumber(ARGV[1])

if redis.call("ZSCORE", KEYS[1], ARGV[2]) == false then
    return 0
end
redis.call("ZADD", KEYS[1], now + lease, ARGV[2])
redis.call("PEXPIRE", KEYS[1], lease + 1000)
return 1
"""

RELEASE_CONCURRENCY_SCRIPT = """
redis.call("ZREM", KEYS[1], ARGV[1])
if redis.call("ZCARD", KEYS[1]) == 0 then
    redis.call("DEL", KEYS[1])
end
return 1
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_after_seconds: int

    def headers(self) -> dict[str, str]:
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset_after_seconds),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers


@dataclass(frozen=True)
class ConcurrencyDecision:
    allowed: bool
    active: int


class LimitStore(Protocol):
    def ping(self) -> bool:
        ...

    def consume_rate_limit(
        self,
        client_id: str,
        request_id: str,
    ) -> RateLimitDecision:
        ...

    def acquire_concurrency(self, request_id: str) -> ConcurrencyDecision:
        ...

    def renew_concurrency(self, request_id: str) -> bool:
        ...

    def release_concurrency(self, request_id: str) -> None:
        ...

    def close(self) -> None:
        ...


class RedisLimitStore:
    """使用 Redis 原子脚本维护共享窗口和并发租约。"""

    def __init__(self, settings: APISettings):
        self._settings = settings
        self._client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
            max_connections=max(8, settings.max_concurrent_requests * 2),
        )
        self._rate_limit = self._client.register_script(RATE_LIMIT_SCRIPT)
        self._acquire = self._client.register_script(ACQUIRE_CONCURRENCY_SCRIPT)
        self._renew = self._client.register_script(RENEW_CONCURRENCY_SCRIPT)
        self._release = self._client.register_script(RELEASE_CONCURRENCY_SCRIPT)

    def ping(self) -> bool:
        return bool(self._client.ping())

    def _rate_key(self, client_id: str) -> str:
        return f"{self._settings.redis_prefix}:rate:{client_id}"

    def _concurrency_key(self) -> str:
        return f"{self._settings.redis_prefix}:inflight"

    def consume_rate_limit(
        self,
        client_id: str,
        request_id: str,
    ) -> RateLimitDecision:
        result = self._rate_limit(
            keys=[self._rate_key(client_id)],
            args=[
                self._settings.rate_limit_window_seconds * 1000,
                self._settings.rate_limit_requests,
                request_id,
            ],
        )
        return RateLimitDecision(
            allowed=bool(result[0]),
            limit=self._settings.rate_limit_requests,
            remaining=int(result[1]),
            retry_after_seconds=int(result[2]),
            reset_after_seconds=int(result[3]),
        )

    def acquire_concurrency(self, request_id: str) -> ConcurrencyDecision:
        result = self._acquire(
            keys=[self._concurrency_key()],
            args=[
                self._settings.concurrency_lease_seconds * 1000,
                self._settings.max_concurrent_requests,
                request_id,
            ],
        )
        return ConcurrencyDecision(allowed=bool(result[0]), active=int(result[1]))

    def renew_concurrency(self, request_id: str) -> bool:
        result = self._renew(
            keys=[self._concurrency_key()],
            args=[
                self._settings.concurrency_lease_seconds * 1000,
                request_id,
            ],
        )
        return bool(result)

    def release_concurrency(self, request_id: str) -> None:
        self._release(
            keys=[self._concurrency_key()],
            args=[request_id],
        )

    def close(self) -> None:
        self._client.close()


class ConcurrencyHeartbeat:
    """在长请求执行期间定期续租，进程退出后租约会自然过期。"""

    def __init__(
        self,
        store: LimitStore,
        request_id: str,
        interval_seconds: float,
    ):
        self._store = store
        self._request_id = request_id
        self._interval_seconds = max(0.1, interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"graph-qa-lease-{self._request_id}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                if not self._store.renew_concurrency(self._request_id):
                    logger.warning("并发租约续租失败，请求将等待租约自然过期")
                    return
            except Exception as exc:
                logger.warning("并发租约续租异常: %s", type(exc).__name__)
                return

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval_seconds + 1.0))
