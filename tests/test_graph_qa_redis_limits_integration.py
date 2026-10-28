# -*- coding: utf-8 -*-
"""共享 Redis 限流与并发租约集成测试。"""

import os
import uuid

import pytest
import redis

from graph_qa.api_security import APISettings
from graph_qa.models import ConversationTurn
from graph_qa.redis_limits import RedisLimitStore
from graph_qa.session_store import RedisConversationStore

REDIS_TEST_URL = os.getenv("GRAPH_QA_REDIS_TEST_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_TEST_URL,
    reason="设置 GRAPH_QA_REDIS_TEST_URL 后才运行共享 Redis 集成测试",
)


def make_settings(prefix: str, **overrides) -> APISettings:
    values = {
        "auth_required": False,
        "redis_url": REDIS_TEST_URL,
        "redis_prefix": prefix,
        "rate_limit_requests": 3,
        "rate_limit_window_seconds": 60,
        "max_concurrent_requests": 2,
        "concurrency_lease_seconds": 30,
    }
    values.update(overrides)
    return APISettings(**values)


def test_rate_limit_is_shared_between_store_instances():
    prefix = f"graph_qa_test_{uuid.uuid4().hex}"
    settings = make_settings(prefix)
    first = RedisLimitStore(settings)
    second = RedisLimitStore(settings)
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    try:
        decisions = [
            first.consume_rate_limit("client-a", "request-1"),
            second.consume_rate_limit("client-a", "request-2"),
            first.consume_rate_limit("client-a", "request-3"),
            second.consume_rate_limit("client-a", "request-4"),
        ]

        assert [decision.allowed for decision in decisions] == [
            True,
            True,
            True,
            False,
        ]
        assert decisions[-1].remaining == 0
        assert decisions[-1].retry_after_seconds >= 1
    finally:
        client.delete(f"{prefix}:rate:client-a")
        first.close()
        second.close()
        client.close()


def test_concurrency_leases_are_shared_between_store_instances():
    prefix = f"graph_qa_test_{uuid.uuid4().hex}"
    settings = make_settings(prefix)
    first = RedisLimitStore(settings)
    second = RedisLimitStore(settings)
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    try:
        assert first.acquire_concurrency("request-1").allowed is True
        assert second.acquire_concurrency("request-2").allowed is True
        assert first.acquire_concurrency("request-3").allowed is False
        assert second.renew_concurrency("request-2") is True

        first.release_concurrency("request-1")
        assert second.acquire_concurrency("request-3").allowed is True
    finally:
        client.delete(f"{prefix}:inflight")
        first.close()
        second.close()
        client.close()


def test_conversation_history_is_shared_trimmed_and_locked():
    prefix = f"graph_qa_test_{uuid.uuid4().hex}"
    settings = make_settings(prefix, session_max_turns=2)
    first = RedisConversationStore(settings)
    second = RedisConversationStore(settings)
    client = redis.Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    session_id = str(uuid.uuid4())
    turns = [
        ConversationTurn(
            question=f"问题 {index}",
            answer=f"回答 {index}",
            grounded=False,
        )
        for index in range(3)
    ]
    try:
        assert first.acquire_lock("client-a", session_id, "lock-1") is True
        assert second.acquire_lock("client-a", session_id, "lock-2") is False
        first.save("client-a", session_id, turns)

        loaded = second.load("client-a", session_id)

        assert [item.question for item in loaded] == ["问题 1", "问题 2"]
        assert client.ttl(f"{prefix}:session:client-a:{session_id}") > 0

        first.release_lock("client-a", session_id, "lock-1")
        assert second.acquire_lock("client-a", session_id, "lock-2") is True
        second.delete("client-a", session_id)
        assert second.load("client-a", session_id) is None
    finally:
        client.delete(
            f"{prefix}:session:client-a:{session_id}",
            f"{prefix}:session-lock:client-a:{session_id}",
        )
        first.close()
        second.close()
        client.close()
