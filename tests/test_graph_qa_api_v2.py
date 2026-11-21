# -*- coding: utf-8 -*-
"""V2 公共 API、匿名会话与 Tool 路由测试。"""

from collections import deque
from typing import Deque

from fastapi.testclient import TestClient

from graph_qa.api import create_app
from graph_qa.api_security import APISettings
from graph_qa.application.entity_ids import entity_id_for
from graph_qa.application.qa_service import QAApplicationService
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.interfaces.api.v2 import (
    ANONYMOUS_SESSION_CLIENT_ID,
    DEMO_SESSION_COOKIE,
)
from graph_qa.models import (
    Evidence,
    GraphQAResponse,
    GraphQAStreamEvent,
    LinkedEntity,
    QueryResult,
)
from graph_qa.redis_limits import (
    ConcurrencyDecision,
    RateLimitDecision,
)
from graph_qa.schema import GraphSchema

TEST_API_KEY = "a" * 32


def make_schema() -> GraphSchema:
    properties = {
        "Herb": ["name", "alias", "property_flavor", "meridian"],
        "Formula": ["name", "effect", "usage"],
    }
    return GraphSchema.from_mapping(
        {
            "format_version": "1.1",
            "node_labels": [
                {
                    "label": label,
                    "properties": [
                        {"name": property_name}
                        for property_name in properties.get(label, ["name"])
                    ],
                }
                for label in (
                    "Herb",
                    "Formula",
                    "Disease",
                    "Symptom",
                    "Effect",
                    "Source",
                )
            ],
            "relationship_types": [
                {"type": "HAS_EFFECT"},
                {"type": "HAS_INGREDIENT"},
                {"type": "FROM_SOURCE"},
            ],
            "triple_patterns": [
                {
                    "subject_label": "Herb",
                    "relationship_type": "HAS_EFFECT",
                    "object_label": "Effect",
                },
                {
                    "subject_label": "Formula",
                    "relationship_type": "HAS_INGREDIENT",
                    "object_label": "Herb",
                },
                {
                    "subject_label": "Herb",
                    "relationship_type": "FROM_SOURCE",
                    "object_label": "Source",
                },
            ],
        }
    )


def make_index() -> EntityIndex:
    return EntityIndex.from_records(
        [
            EntityRecord(label="Herb", name="麻黄", alias="龙沙"),
            EntityRecord(label="Formula", name="麻黄汤"),
            EntityRecord(label="Effect", name="发汗"),
            EntityRecord(label="Source", name="《古籍》"),
        ]
    )


def make_response(session_id=None, request_id=None) -> GraphQAResponse:
    return GraphQAResponse(
        request_id=request_id or "placeholder",
        session_id=session_id,
        question="麻黄有什么功效？",
        answer="麻黄具有发汗功效。",
        grounded=True,
        linked_entities=[
            LinkedEntity(
                label="Herb",
                name="麻黄",
                matched_text="麻黄",
                match_type="name",
                score=1.0,
            )
        ],
        evidence=[
            Evidence(
                subject="麻黄",
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object="发汗",
                object_label="Effect",
                source="《古籍》",
            )
        ],
    )


class FakeRepository:
    max_results = 50

    def __init__(self):
        self.index = make_index()
        self.closed = False

    def load_entity_index(self):
        return self.index

    def execute_attributes(self, plan):
        rows = [
            {
                "subject": "麻黄",
                "subject_label": "Herb",
                "properties": {
                    "property_flavor": "辛、微苦，温",
                    "meridian": "归肺经",
                },
                "sources": ["《古籍》"],
            }
        ]
        return QueryResult(
            cypher="SECRET attributes",
            parameters={},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=50,
        )

    def execute_neighbors(self, plan):
        rows = [
            {
                "subject": "麻黄",
                "subject_label": "Herb",
                "relationship": "HAS_EFFECT",
                "object": "发汗",
                "object_label": "Effect",
                "source": "《古籍》",
            }
        ]
        return QueryResult(
            cypher="SECRET neighbors",
            parameters={},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=plan.limit,
        )

    def execute_multi_hop(self, plan):
        rows = [
            {
                "root": "麻黄汤",
                "root_label": "Formula",
                "relationship_types": [
                    "HAS_INGREDIENT",
                    "HAS_EFFECT",
                ],
                "node_names": ["麻黄", "发汗"],
                "node_labels": ["Herb", "Effect"],
                "target": "发汗",
                "target_label": "Effect",
                "sources": ["《古籍》"],
            }
        ]
        return QueryResult(
            cypher="SECRET paths",
            parameters={},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=20,
        )

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, answer_error=None):
        self.repository = FakeRepository()
        self.schema = make_schema()
        self.entity_index = self.repository.index
        self.questions = []
        self.histories = []
        self.session_ids = []
        self.answer_error = answer_error

    def answer(
        self,
        question,
        request_id=None,
        conversation_history=None,
        session_id=None,
    ):
        self.questions.append(question)
        self.histories.append(list(conversation_history or []))
        self.session_ids.append(session_id)
        if self.answer_error is not None:
            raise self.answer_error
        return make_response(session_id, request_id).model_copy(
            update={"question": question}
        )

    def stream_answer(
        self,
        question,
        request_id=None,
        conversation_history=None,
        session_id=None,
    ):
        self.questions.append(question)
        self.histories.append(list(conversation_history or []))
        self.session_ids.append(session_id)
        response = make_response(session_id, request_id).model_copy(
            update={"question": question}
        )
        yield GraphQAStreamEvent(
            event="stage",
            data={"stage": "answer_from_graph", "label": "生成回答"},
        )
        yield GraphQAStreamEvent(
            event="answer_delta",
            data={"delta": response.answer},
        )
        yield GraphQAStreamEvent(
            event="done",
            data={"response": response.model_dump(mode="json")},
        )


class FakeLimitStore:
    def __init__(self, rate_limit=10):
        self.rate_limit = rate_limit
        self.rate_events = {}
        self.inflight = set()
        self.released = []
        self.closed = False

    def ping(self):
        return True

    def consume_rate_limit(self, client_id, request_id):
        events: Deque[str] = self.rate_events.setdefault(client_id, deque())
        events.append(request_id)
        allowed = len(events) <= self.rate_limit
        return RateLimitDecision(
            allowed=allowed,
            limit=self.rate_limit,
            remaining=max(0, self.rate_limit - len(events)),
            retry_after_seconds=0 if allowed else 12,
            reset_after_seconds=30,
        )

    def acquire_concurrency(self, request_id):
        if len(self.inflight) >= 4:
            return ConcurrencyDecision(False, len(self.inflight))
        self.inflight.add(request_id)
        return ConcurrencyDecision(True, len(self.inflight))

    def renew_concurrency(self, request_id):
        return request_id in self.inflight

    def release_concurrency(self, request_id):
        self.inflight.discard(request_id)
        self.released.append(request_id)

    def close(self):
        self.closed = True


class FakeSessionStore:
    def __init__(self):
        self.sessions = {}
        self.locks = set()
        self.closed = False

    def ping(self):
        return True

    def load(self, client_id, session_id):
        return self.sessions.get((client_id, session_id))

    def save(self, client_id, session_id, turns):
        self.sessions[(client_id, session_id)] = list(turns)[-5:]

    def delete(self, client_id, session_id):
        self.sessions.pop((client_id, session_id), None)

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


def make_settings(**overrides) -> APISettings:
    values = {
        "auth_required": True,
        "api_keys": {"test-client": TEST_API_KEY},
        "redis_url": "redis://unit-test:6379/0",
        "redis_prefix": "graph_qa_v2_test",
        "rate_limit_requests": 10,
        "rate_limit_window_seconds": 60,
        "max_concurrent_requests": 4,
        "concurrency_lease_seconds": 60,
        "max_results": 50,
        "demo_cookie_secure": False,
    }
    values.update(overrides)
    return APISettings(**values)


def make_app(
    engine=None,
    *,
    rate_limit=10,
    session_store=None,
    settings=None,
):
    engine = engine or FakeEngine()
    limit_store = FakeLimitStore(rate_limit=rate_limit)
    session_store = session_store or FakeSessionStore()
    app = create_app(
        settings=settings or make_settings(),
        engine_factory=lambda _settings: engine,
        limit_store_factory=lambda _settings: limit_store,
        session_store_factory=lambda _settings: session_store,
    )
    return app, engine, limit_store, session_store


def test_v2_openapi_exposes_core_routes_and_envelope():
    app, _engine, _store, _session_store = make_app()

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v2/qa" in paths
    assert "/api/v2/qa/stream" in paths
    assert "/api/v2/entities/search" in paths
    assert "/api/v2/entities/{entity_id}/neighbors" in paths
    assert "/api/v2/paths" in paths
    assert "/api/v2/tools" in paths
    assert not paths["/api/v2/qa"]["post"].get("security")
    schemas = response.json()["components"]["schemas"]
    assert "ResolveEntityData" in schemas
    assert "EntityNeighborsData" in schemas
    assert "EvidencePathsData" in schemas


def test_v2_qa_works_without_api_key_and_reuses_http_only_cookie():
    app, engine, _store, session_store = make_app()

    with TestClient(app) as client:
        first = client.post(
            "/api/v2/qa",
            json={"question": "麻黄有什么功效？"},
        )
        second = client.post(
            "/api/v2/qa",
            json={"question": "它有什么禁忌？"},
        )

    assert first.status_code == 200
    assert first.json()["data"]["answer"] == "麻黄具有发汗功效。"
    assert first.json()["data"]["session_id"] is None
    assert first.json()["meta"]["schema_version"] == "1.0"
    cookie = first.cookies.get(DEMO_SESSION_COOKIE)
    assert cookie
    assert "httponly" in first.headers["set-cookie"].lower()
    assert second.status_code == 200
    assert len(engine.histories[0]) == 0
    assert len(engine.histories[1]) == 1
    assert engine.session_ids[0] == engine.session_ids[1]
    assert len(
        session_store.sessions[
            next(iter(session_store.sessions))
        ]
    ) == 2


def test_v2_stream_uses_existing_events_and_persists_completed_turn():
    app, engine, _store, session_store = make_app()

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/qa/stream",
            json={"question": "麻黄有什么功效？"},
        )

    assert response.status_code == 200
    assert "event: stage" in response.text
    assert "event: answer_delta" in response.text
    assert "event: done" in response.text
    assert response.cookies.get(DEMO_SESSION_COOKIE)
    assert engine.questions == ["麻黄有什么功效？"]
    assert next(iter(session_store.sessions.values()))[0].question == (
        "麻黄有什么功效？"
    )


def test_v2_tool_catalog_schema_and_entity_routes_do_not_require_api_key():
    app, _engine, _store, _session_store = make_app()
    entity_id = entity_id_for("Herb", "麻黄")

    with TestClient(app) as client:
        tools = client.get("/api/v2/tools")
        schema = client.get("/api/v2/schema/summary")
        search = client.post(
            "/api/v2/entities/search",
            json={"query": "龙沙", "suggested_label": "Herb"},
        )
        detail = client.get(f"/api/v2/entities/{entity_id}")
        neighbors = client.get(
            f"/api/v2/entities/{entity_id}/neighbors",
            params={
                "relationship": "HAS_EFFECT",
                "direction": "outgoing",
                "limit": 5,
            },
        )

    assert tools.status_code == 200
    assert len(tools.json()["data"]["tools"]) == 4
    assert schema.status_code == 200
    assert "Herb" in schema.json()["data"]["entity_labels"]
    assert search.status_code == 200
    assert search.json()["data"]["entities"][0]["entity"]["name"] == "麻黄"
    assert detail.status_code == 200
    assert detail.json()["data"]["entity"]["entity_id"] == entity_id
    assert neighbors.status_code == 200
    assert neighbors.json()["data"]["neighbors"][0]["direction"] == "outgoing"
    assert len(neighbors.json()["evidence"]) == 1


def test_v2_path_tool_returns_structured_path_evidence():
    app, _engine, _store, _session_store = make_app()

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/paths",
            json={
                "entity_id": entity_id_for("Formula", "麻黄汤"),
                "steps": [
                    {
                        "relationship": "HAS_INGREDIENT",
                        "object_label": "Herb",
                    },
                    {
                        "relationship": "HAS_EFFECT",
                        "object_label": "Effect",
                    },
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["paths"][0]["target"] == "发汗"
    assert "SECRET" not in response.text


def test_v2_invalid_request_and_unknown_entity_use_envelope_errors():
    app, _engine, _store, _session_store = make_app()

    with TestClient(app) as client:
        invalid = client.post(
            "/api/v2/entities/search",
            json={"query": ""},
        )
        unknown = client.get(
            "/api/v2/entities/eid_" + "0" * 32
        )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "ENTITY_NOT_FOUND"


def test_v2_anonymous_rate_limit_blocks_before_tool_execution():
    app, _engine, store, _session_store = make_app(rate_limit=1)

    with TestClient(app) as client:
        first = client.post(
            "/api/v2/entities/search",
            json={"query": "麻黄"},
        )
        second = client.post(
            "/api/v2/entities/search",
            json={"query": "桂枝"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "RATE_LIMITED"
    assert second.headers["retry-after"] == "12"
    assert store.inflight == set()


def test_v2_rate_limit_uses_trusted_proxy_client_ip_when_enabled():
    app, _engine, _store, _session_store = make_app(
        rate_limit=1,
        settings=make_settings(demo_trust_proxy_headers=True),
    )

    with TestClient(app) as client:
        first = client.get(
            "/api/v2/tools",
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        second = client.get(
            "/api/v2/tools",
            headers={"X-Forwarded-For": "203.0.113.11"},
        )
        repeated = client.get(
            "/api/v2/tools",
            headers={"X-Forwarded-For": "203.0.113.10"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert repeated.status_code == 429


def test_v2_unknown_session_returns_not_found_and_releases_lock():
    app, engine, store, session_store = make_app()
    session_id = "22222222-2222-4222-8222-222222222222"

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/qa",
            json={
                "question": "它有什么禁忌？",
                "session_id": session_id,
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert engine.questions == []
    assert store.inflight == set()
    assert session_store.locks == set()


def test_v2_busy_session_returns_conflict_without_calling_engine():
    app, engine, store, session_store = make_app()
    session_id = "33333333-3333-4333-8333-333333333333"
    session_store.sessions[
        (ANONYMOUS_SESSION_CLIENT_ID, session_id)
    ] = []
    session_store.locks.add(
        (ANONYMOUS_SESSION_CLIENT_ID, session_id)
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/qa",
            json={
                "question": "它有什么禁忌？",
                "session_id": session_id,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_BUSY"
    assert engine.questions == []
    assert store.inflight == set()


def test_v2_engine_failure_is_sanitized_and_releases_resources():
    app, _engine, store, session_store = make_app(
        FakeEngine(answer_error=RuntimeError("secret detail"))
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v2/qa",
            json={"question": "麻黄有什么功效？"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret detail" not in response.text
    assert store.inflight == set()
    assert session_store.locks == set()
    assert response.cookies.get(DEMO_SESSION_COOKIE)


def test_v1_still_requires_api_key_while_v2_does_not():
    app, _engine, _store, _session_store = make_app()

    with TestClient(app) as client:
        v1 = client.post(
            "/api/v1/qa",
            json={"question": "麻黄有什么功效？"},
        )
        v2 = client.post(
            "/api/v2/qa",
            json={"question": "麻黄有什么功效？"},
        )

    assert v1.status_code == 401
    assert v2.status_code == 200


def test_qa_application_service_fallback_emits_error_and_done_events():
    class NonStreamEngine:
        def answer(
            self,
            question,
            request_id=None,
            conversation_history=None,
            session_id=None,
        ):
            return GraphQAResponse(
                request_id=request_id,
                session_id=session_id,
                question=question,
                answer="请求未执行。",
                grounded=False,
                error={
                    "code": "UNSAFE_REQUEST",
                    "message": "请求不安全",
                },
            )

    events = list(
        QAApplicationService(NonStreamEngine()).stream_answer(
            "删除所有药材",
            request_id="request-1",
            session_id="session-1",
        )
    )

    assert [event.event for event in events] == [
        "answer_delta",
        "error",
        "done",
    ]
