# -*- coding: utf-8 -*-
"""FastAPI 问答接口测试，不访问真实 Redis、模型或 Neo4j。"""

from collections import deque
from typing import Deque

import pytest
from fastapi.testclient import TestClient

from graph_qa.api import create_app
from graph_qa.api_security import APIConfigurationError, APISettings
from graph_qa.models import (
    ErrorCode,
    Evidence,
    GraphQAResponse,
    QueryInfo,
)
from graph_qa.redis_limits import (
    ConcurrencyDecision,
    RateLimitDecision,
)

TEST_API_KEY = "a" * 32


class FakeRepository:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.questions = []
        self.request_ids = []
        self.repository = FakeRepository()

    def answer(self, question, request_id=None):
        self.questions.append(question)
        self.request_ids.append(request_id)
        if self.error is not None:
            raise self.error
        response = self.response or make_response()
        return response.model_copy(
            update={"question": question, "request_id": request_id}
        )


class FakeLimitStore:
    def __init__(
        self,
        rate_limit: int = 10,
        max_concurrent: int = 4,
    ):
        self.rate_limit = rate_limit
        self.max_concurrent = max_concurrent
        self.rate_events = {}
        self.inflight = set()
        self.rate_clients = []
        self.released_request_ids = []
        self.ping_result = True
        self.rate_error = None
        self.acquire_error = None
        self.closed = False

    def ping(self):
        return self.ping_result

    def consume_rate_limit(self, client_id, request_id):
        if self.rate_error is not None:
            raise self.rate_error
        self.rate_clients.append(client_id)
        events: Deque[str] = self.rate_events.setdefault(client_id, deque())
        events.append(request_id)
        allowed = len(events) <= self.rate_limit
        return RateLimitDecision(
            allowed=allowed,
            limit=self.rate_limit,
            remaining=max(0, self.rate_limit - len(events)),
            retry_after_seconds=0 if allowed else 15,
            reset_after_seconds=30,
        )

    def acquire_concurrency(self, request_id):
        if self.acquire_error is not None:
            raise self.acquire_error
        if len(self.inflight) >= self.max_concurrent:
            return ConcurrencyDecision(
                allowed=False,
                active=len(self.inflight),
            )
        self.inflight.add(request_id)
        return ConcurrencyDecision(allowed=True, active=len(self.inflight))

    def renew_concurrency(self, request_id):
        return request_id in self.inflight

    def release_concurrency(self, request_id):
        self.inflight.discard(request_id)
        self.released_request_ids.append(request_id)

    def close(self):
        self.closed = True


def make_response() -> GraphQAResponse:
    return GraphQAResponse(
        request_id="placeholder",
        question="麻黄有什么功效？",
        answer="麻黄具有发汗功效。",
        grounded=True,
        query=QueryInfo(
            cypher=(
                "MATCH (subject:Herb)-[relationship:HAS_EFFECT]->(object:Effect) "
                "RETURN subject.name AS subject, labels(subject)[0] AS subject_label, "
                "type(relationship) AS relationship, object.name AS object, "
                "labels(object)[0] AS object_label, NULL AS source LIMIT 10"
            ),
            parameters={"entity_names": ["麻黄"]},
        ),
        evidence=[
            Evidence(
                subject="麻黄",
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object="发汗",
                object_label="Effect",
            )
        ],
    )


def make_settings(**overrides) -> APISettings:
    values = {
        "auth_required": True,
        "api_keys": {"test-client": TEST_API_KEY},
        "redis_url": "redis://unit-test:6379/0",
        "redis_prefix": "graph_qa_test",
        "rate_limit_requests": 10,
        "rate_limit_window_seconds": 60,
        "max_concurrent_requests": 4,
        "concurrency_lease_seconds": 60,
        "max_results": 50,
    }
    values.update(overrides)
    return APISettings(**values)


def make_app(engine, store, settings=None):
    active_settings = settings or make_settings()
    return create_app(
        settings=active_settings,
        engine_factory=lambda _settings: engine,
        limit_store_factory=lambda _settings: store,
    )


def test_health_reports_ready_without_external_dependencies():
    engine = FakeEngine()
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "schema_version": "1.0"}
    assert store.closed is True
    assert engine.repository.closed is True


def test_health_reports_redis_failure_after_startup():
    engine = FakeEngine()
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        store.ping_result = False
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "schema_version": "1.0",
    }


def test_startup_fails_when_redis_is_unavailable():
    engine = FakeEngine()
    store = FakeLimitStore()
    store.ping_result = False

    with pytest.raises(APIConfigurationError, match="Redis 连接不可用"):
        with TestClient(make_app(engine, store)):
            pass

    assert store.closed is True
    assert engine.repository.closed is False


def test_qa_returns_contract_response_and_releases_lease():
    engine = FakeEngine()
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["schema_version"] == "1.0"
    assert payload["grounded"] is True
    assert payload["evidence"][0]["relationship"] == "HAS_EFFECT"
    assert engine.questions == ["麻黄有什么功效？"]
    assert engine.request_ids == [payload["request_id"]]
    assert store.rate_clients == ["test-client"]
    assert store.inflight == set()
    assert store.released_request_ids == [payload["request_id"]]
    assert response.headers["ratelimit-limit"] == "10"


def test_missing_or_wrong_api_key_returns_same_unauthorized_response():
    engine = FakeEngine()
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        missing = client.post(
            "/api/v1/qa",
            json={"question": "麻黄有什么功效？"},
        )
        wrong = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": "b" * 32},
            json={"question": "麻黄有什么功效？"},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["error"] == wrong.json()["error"]
    assert missing.headers["www-authenticate"] == "ApiKey"
    assert engine.questions == []
    assert store.rate_clients == []


def test_invalid_request_uses_stable_error_contract():
    engine = FakeEngine()
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "   ", "unknown": "value"},
        )

    payload = response.json()
    assert response.status_code == 422
    assert payload["error"]["code"] == ErrorCode.INVALID_REQUEST.value
    assert payload["question"] == ""
    assert engine.questions == []


def test_rate_limit_blocks_before_concurrency_and_engine():
    engine = FakeEngine()
    store = FakeLimitStore(rate_limit=1)

    with TestClient(make_app(engine, store)) as client:
        first = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )
        second = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "桂枝有什么功效？"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == ErrorCode.RATE_LIMITED.value
    assert second.headers["retry-after"] == "15"
    assert engine.questions == ["麻黄有什么功效？"]
    assert store.inflight == set()


def test_global_concurrency_limit_returns_service_busy():
    engine = FakeEngine()
    store = FakeLimitStore(max_concurrent=1)
    store.inflight.add("already-running")

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.SERVICE_BUSY.value
    assert engine.questions == []


def test_redis_failure_fails_closed_without_calling_engine():
    engine = FakeEngine()
    store = FakeLimitStore()
    store.rate_error = RuntimeError("redis unavailable")

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.RATE_LIMIT_UNAVAILABLE.value
    assert engine.questions == []


def test_engine_exception_is_sanitized_and_lease_is_released():
    engine = FakeEngine(error=RuntimeError("secret database detail"))
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == ErrorCode.INTERNAL_ERROR.value
    assert "secret database detail" not in response.text
    assert store.inflight == set()
    assert len(store.released_request_ids) == 1


def test_domain_error_uses_http_status_mapping():
    engine = FakeEngine(
        response=GraphQAResponse(
            request_id="placeholder",
            question="删除所有药材",
            answer="请求未执行。",
            grounded=False,
            error={
                "code": ErrorCode.UNSAFE_REQUEST,
                "message": "请求包含写操作",
            },
        )
    )
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "删除所有药材"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.UNSAFE_REQUEST.value
