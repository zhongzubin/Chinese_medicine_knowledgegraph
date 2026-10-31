# -*- coding: utf-8 -*-
"""FastAPI 问答接口测试，不访问真实 Redis、模型或 Neo4j。"""

from collections import deque
import json
from typing import Deque

import pytest
from fastapi.testclient import TestClient

from graph_qa.api import create_app
from graph_qa.api_security import APIConfigurationError, APISettings
from graph_qa.models import (
    AggregateFact,
    AttributeFact,
    ConversationTurn,
    ErrorCode,
    Evidence,
    GraphQAResponse,
    PathEvidence,
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
        self.histories = []
        self.session_ids = []
        self.repository = FakeRepository()

    def answer(
        self,
        question,
        request_id=None,
        conversation_history=None,
        session_id=None,
    ):
        self.questions.append(question)
        self.request_ids.append(request_id)
        self.histories.append(list(conversation_history or []))
        self.session_ids.append(session_id)
        if self.error is not None:
            raise self.error
        response = self.response or make_response()
        return response.model_copy(
            update={
                "question": question,
                "request_id": request_id,
                "session_id": session_id,
            }
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


class FakeSessionStore:
    def __init__(self):
        self.sessions = {}
        self.locks = set()
        self.deleted = []
        self.ping_result = True
        self.load_error = None
        self.save_error = None
        self.closed = False

    def ping(self):
        return self.ping_result

    def load(self, client_id, session_id):
        if self.load_error is not None:
            raise self.load_error
        return self.sessions.get((client_id, session_id))

    def save(self, client_id, session_id, turns):
        if self.save_error is not None:
            raise self.save_error
        self.sessions[(client_id, session_id)] = list(turns)[-5:]

    def delete(self, client_id, session_id):
        self.sessions.pop((client_id, session_id), None)
        self.deleted.append((client_id, session_id))

    def acquire_lock(self, client_id, session_id, lock_token):
        key = (client_id, session_id)
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    def release_lock(self, client_id, session_id, lock_token):
        self.locks.discard((client_id, session_id))

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


def make_app(engine, store, settings=None, session_store=None):
    active_settings = settings or make_settings()
    active_session_store = session_store or FakeSessionStore()
    return create_app(
        settings=active_settings,
        engine_factory=lambda _settings: engine,
        limit_store_factory=lambda _settings: store,
        session_store_factory=lambda _settings: active_session_store,
    )


def test_health_reports_ready_without_external_dependencies():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()

    with TestClient(make_app(engine, store, session_store=session_store)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "schema_version": "1.0"}
    assert store.closed is True
    assert session_store.closed is True
    assert engine.repository.closed is True


def test_health_reports_redis_failure_after_startup():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()

    with TestClient(make_app(engine, store, session_store=session_store)) as client:
        session_store.ping_result = False
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "schema_version": "1.0",
    }


def test_startup_fails_when_redis_is_unavailable():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()
    store.ping_result = False

    with pytest.raises(APIConfigurationError, match="Redis 连接不可用"):
        with TestClient(make_app(engine, store, session_store=session_store)):
            pass

    assert store.closed is True
    assert session_store.closed is True
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
    assert payload["session_id"]
    assert engine.questions == ["麻黄有什么功效？"]
    assert engine.request_ids == [payload["request_id"]]
    assert engine.histories == [[]]
    assert store.rate_clients == ["test-client"]
    assert store.inflight == set()
    assert store.released_request_ids == [payload["request_id"]]
    assert response.headers["ratelimit-limit"] == "10"


def test_qa_serializes_attribute_facts():
    engine = FakeEngine(
        response=GraphQAResponse(
            question="麻黄性味是什么？",
            answer="麻黄性味为辛、微苦，温。",
            grounded=True,
            facts=[
                AttributeFact(
                    subject="麻黄",
                    subject_label="Herb",
                    attribute="property_flavor",
                    attribute_label="性味",
                    value="辛、微苦，温",
                )
            ],
        )
    )
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄性味是什么？"},
        )

    assert response.status_code == 200
    assert response.json()["facts"][0]["attribute_label"] == "性味"


def test_qa_serializes_aggregate_facts():
    engine = FakeEngine(
        response=GraphQAResponse(
            question="含有人参的方剂有多少种？",
            answer="根据当前图谱统计，关联到人参的方剂共有 7 个。",
            grounded=True,
            aggregates=[
                AggregateFact(
                    anchor="人参",
                    anchor_label="Herb",
                    relationship="HAS_INGREDIENT",
                    result_label="Formula",
                    direction="incoming",
                    count=7,
                    description="关联到人参的方剂",
                )
            ],
        )
    )
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "含有人参的方剂有多少种？"},
        )

    assert response.status_code == 200
    assert response.json()["aggregates"][0]["count"] == 7


def test_qa_serializes_multi_hop_paths():
    engine = FakeEngine(
        response=GraphQAResponse(
            question="四君子汤所含药材有什么功效？",
            answer="四君子汤所含人参具有补气功效。",
            grounded=True,
            paths=[
                PathEvidence(
                    root="四君子汤",
                    root_label="Formula",
                    first_relationship="HAS_INGREDIENT",
                    intermediate="人参",
                    intermediate_label="Herb",
                    second_relationship="HAS_EFFECT",
                    target="补气",
                    target_label="Effect",
                )
            ],
        )
    )
    store = FakeLimitStore()

    with TestClient(make_app(engine, store)) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "四君子汤所含药材有什么功效？"},
        )

    assert response.status_code == 200
    assert response.json()["paths"][0]["intermediate"] == "人参"


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


def test_existing_session_loads_five_turn_history():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()
    session_id = "11111111-1111-4111-8111-111111111111"
    previous = ConversationTurn(
        question="麻黄有什么功效？",
        answer="麻黄具有发汗功效。",
        linked_entities=make_response().linked_entities,
        grounded=True,
    )
    session_store.sessions[("test-client", session_id)] = [previous]

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={
                "question": "它有什么禁忌？",
                "session_id": session_id,
            },
        )

    assert response.status_code == 200
    assert response.json()["session_id"] == session_id
    assert engine.histories == [[previous]]
    assert len(session_store.sessions[("test-client", session_id)]) == 2
    assert session_store.locks == set()


def test_unknown_or_other_client_session_is_not_found():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()
    session_id = "22222222-2222-4222-8222-222222222222"
    session_store.sessions[("other-client", session_id)] = [
        ConversationTurn(
            question="麻黄有什么功效？",
            answer="已回答。",
            grounded=True,
        )
    ]

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={
                "question": "它有什么禁忌？",
                "session_id": session_id,
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.SESSION_NOT_FOUND.value
    assert engine.questions == []


def test_busy_session_returns_conflict_without_calling_engine():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()
    session_id = "33333333-3333-4333-8333-333333333333"
    session_store.sessions[("test-client", session_id)] = []
    session_store.locks.add(("test-client", session_id))

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        response = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={
                "question": "它有什么禁忌？",
                "session_id": session_id,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.SESSION_BUSY.value
    assert engine.questions == []


def test_session_delete_is_scoped_to_client():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()
    session_id = "44444444-4444-4444-8444-444444444444"
    session_store.sessions[("test-client", session_id)] = []

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        response = client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-API-Key": TEST_API_KEY},
        )

    assert response.status_code == 204
    assert ("test-client", session_id) not in session_store.sessions


def test_busy_session_cannot_be_deleted():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()
    session_id = "55555555-5555-4555-8555-555555555555"
    session_store.sessions[("test-client", session_id)] = []
    session_store.locks.add(("test-client", session_id))

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        response = client.delete(
            f"/api/v1/sessions/{session_id}",
            headers={"X-API-Key": TEST_API_KEY},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.SESSION_BUSY.value
    assert ("test-client", session_id) in session_store.sessions


def test_stream_endpoint_returns_sse_and_persists_completed_turn():
    engine = FakeEngine()
    store = FakeLimitStore()
    session_store = FakeSessionStore()

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        response = client.post(
            "/api/v1/qa/stream",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: stage" in response.text
    assert "event: answer_delta" in response.text
    assert "event: done" in response.text
    assert len(session_store.sessions) == 1


def test_failed_first_turn_leaves_reusable_empty_session():
    engine = FakeEngine(
        response=GraphQAResponse(
            request_id="placeholder",
            question="麻黄有什么功效？",
            answer="图谱查询失败，请稍后重试。",
            grounded=False,
            error={
                "code": ErrorCode.INTERNAL_ERROR,
                "message": "测试失败",
            },
        )
    )
    store = FakeLimitStore()
    session_store = FakeSessionStore()

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        failed = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )
        session_id = failed.json()["session_id"]

        engine.response = None
        recovered = client.post(
            "/api/v1/qa",
            headers={"X-API-Key": TEST_API_KEY},
            json={
                "question": "桂枝有什么功效？",
                "session_id": session_id,
            },
        )

    assert session_id
    assert failed.status_code == 500
    assert recovered.status_code == 200
    turns = session_store.sessions[("test-client", session_id)]
    assert len(turns) == 1
    assert turns[0].question == "桂枝有什么功效？"


def test_failed_first_stream_turn_leaves_reusable_session():
    engine = FakeEngine(
        response=GraphQAResponse(
            request_id="placeholder",
            question="麻黄有什么功效？",
            answer="图谱查询失败，请稍后重试。",
            grounded=False,
            error={
                "code": ErrorCode.INTERNAL_ERROR,
                "message": "测试失败",
            },
        )
    )
    store = FakeLimitStore()
    session_store = FakeSessionStore()

    with TestClient(
        make_app(engine, store, session_store=session_store)
    ) as client:
        failed = client.post(
            "/api/v1/qa/stream",
            headers={"X-API-Key": TEST_API_KEY},
            json={"question": "麻黄有什么功效？"},
        )
        done_payload = [
            json.loads(line.removeprefix("data: "))
            for line in failed.text.splitlines()
            if line.startswith("data: ")
        ][-1]
        session_id = done_payload["response"]["session_id"]

        engine.response = None
        recovered = client.post(
            "/api/v1/qa/stream",
            headers={"X-API-Key": TEST_API_KEY},
            json={
                "question": "桂枝有什么功效？",
                "session_id": session_id,
            },
        )

    assert failed.status_code == 200
    assert recovered.status_code == 200
    turns = session_store.sessions[("test-client", session_id)]
    assert len(turns) == 1
    assert turns[0].question == "桂枝有什么功效？"
