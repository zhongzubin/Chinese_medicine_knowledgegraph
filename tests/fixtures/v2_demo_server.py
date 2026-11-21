# -*- coding: utf-8 -*-
"""确定性 V2 API fixture，用于 Stage 3 真实浏览器验收。"""

from collections import deque
from typing import Deque

import uvicorn

from graph_qa.api import create_app
from graph_qa.api_security import APISettings
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.models import (
    AnswerVerification,
    AttributeFact,
    Evidence,
    GraphQAResponse,
    GraphQAStreamEvent,
    LinkedEntity,
    PathEvidence,
    PathStepEvidence,
    QueryResult,
)
from graph_qa.redis_limits import (
    ConcurrencyDecision,
    RateLimitDecision,
)
from graph_qa.schema import GraphSchema


def make_schema() -> GraphSchema:
    return GraphSchema.from_mapping(
        {
            "format_version": "1.1",
            "node_labels": [
                {
                    "label": label,
                    "properties": [
                        {"name": "name"},
                        *(
                            [{"name": "property_flavor"}]
                            if label == "Herb"
                            else []
                        ),
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


INDEX = EntityIndex.from_records(
    [
        EntityRecord(label="Herb", name="麻黄", alias="龙沙"),
        EntityRecord(label="Herb", name="桂枝", alias=None),
        EntityRecord(label="Formula", name="麻黄汤", alias=None),
        EntityRecord(label="Formula", name="四君子汤", alias=None),
        EntityRecord(label="Effect", name="发汗", alias=None),
        EntityRecord(label="Effect", name="补气", alias=None),
        EntityRecord(label="Source", name="《本草纲目》", alias=None),
    ]
)


class DemoRepository:
    max_results = 50

    def __init__(self):
        self.schema = make_schema()

    def load_entity_index(self):
        return INDEX

    def execute_attributes(self, plan):
        rows = [
            {
                "subject": "麻黄",
                "subject_label": "Herb",
                "properties": {"property_flavor": "辛、微苦，温"},
                "sources": ["《本草纲目》"],
            }
        ]
        return QueryResult(
            cypher="fixture",
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
                "source": "《本草纲目》",
            }
        ]
        return QueryResult(
            cypher="fixture",
            parameters={},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=plan.limit,
        )

    def execute_multi_hop(self, plan):
        rows = [
            {
                "root": "四君子汤",
                "root_label": "Formula",
                "relationship_types": [
                    "HAS_INGREDIENT",
                    "HAS_EFFECT",
                ],
                "node_names": ["人参", "补气"],
                "node_labels": ["Herb", "Effect"],
                "target": "补气",
                "target_label": "Effect",
                "sources": ["《太平惠民和剂局方》"],
            }
        ]
        return QueryResult(
            cypher="fixture",
            parameters={},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=20,
        )

    def close(self):
        return None


def make_response(
    question: str,
    request_id: str,
    session_id: str,
) -> GraphQAResponse:
    is_multi_hop = "四君子汤" in question
    evidence = [
        Evidence(
            subject="四君子汤" if is_multi_hop else "麻黄",
            subject_label="Formula" if is_multi_hop else "Herb",
            relationship="HAS_INGREDIENT" if is_multi_hop else "HAS_EFFECT",
            object="人参" if is_multi_hop else "发汗",
            object_label="Herb" if is_multi_hop else "Effect",
            source="《太平惠民和剂局方》"
            if is_multi_hop
            else "《本草纲目》",
        )
    ]
    facts = [
        AttributeFact(
            subject="麻黄",
            subject_label="Herb",
            attribute="property_flavor",
            attribute_label="性味",
            value="辛、微苦，温",
            source="《本草纲目》",
        )
    ]
    paths = []
    answer = "根据当前图谱，麻黄具有发汗功效，性味为辛、微苦，温。"
    if is_multi_hop:
        answer = "根据当前图谱，四君子汤含有人参，其关联功效为补气。"
        paths = [
            PathEvidence(
                root="四君子汤",
                root_label="Formula",
                target="补气",
                target_label="Effect",
                source="《太平惠民和剂局方》",
                steps=[
                    PathStepEvidence(
                        subject="四君子汤",
                        subject_label="Formula",
                        relationship="HAS_INGREDIENT",
                        object="人参",
                        object_label="Herb",
                    ),
                    PathStepEvidence(
                        subject="人参",
                        subject_label="Herb",
                        relationship="HAS_EFFECT",
                        object="补气",
                        object_label="Effect",
                    ),
                ],
            )
        ]
    return GraphQAResponse(
        request_id=request_id,
        session_id=session_id,
        question=question,
        answer=answer,
        grounded=True,
        linked_entities=[
            LinkedEntity(
                label="Formula" if is_multi_hop else "Herb",
                name="四君子汤" if is_multi_hop else "麻黄",
                matched_text="四君子汤" if is_multi_hop else "麻黄",
                match_type="name",
                score=1.0,
            )
        ],
        query=None,
        evidence=evidence,
        facts=[] if is_multi_hop else facts,
        paths=paths,
        verification=AnswerVerification(
            status="passed",
            checked_items=len(evidence) + len(paths)
            + (0 if is_multi_hop else len(facts)),
            matched_items=len(evidence) + len(paths)
            + (0 if is_multi_hop else len(facts)),
            evidence_items=len(evidence),
            fact_items=0 if is_multi_hop else len(facts),
            aggregate_items=0,
            path_items=len(paths),
            missing_sources=0,
            fallback_used=False,
            message="已核对本轮全部图谱证据。",
        ),
    )


class DemoEngine:
    def __init__(self):
        self.repository = DemoRepository()
        self.schema = self.repository.schema
        self.entity_index = INDEX

    def answer(
        self,
        question,
        request_id=None,
        conversation_history=None,
        session_id=None,
    ):
        return make_response(question, request_id, session_id)

    def stream_answer(
        self,
        question,
        request_id=None,
        conversation_history=None,
        session_id=None,
    ):
        response = make_response(question, request_id, session_id)
        for stage, label, agent in (
            ("normalize", "规范化问题", "dialogue"),
            ("link_entities", "匹配图谱实体", "entity"),
            ("plan_cypher", "选择受控查询", "retrieval"),
            ("execute_cypher", "执行只读查询", "retrieval"),
            ("verify_answer", "校验回答证据", "evidence_audit"),
        ):
            yield GraphQAStreamEvent(
                event="stage",
                data={"stage": stage, "label": label, "agent": agent},
            )
        yield GraphQAStreamEvent(
            event="evidence",
            data={"items": [item.model_dump(mode="json") for item in response.evidence]},
        )
        yield GraphQAStreamEvent(
            event="facts",
            data={"items": [item.model_dump(mode="json") for item in response.facts]},
        )
        yield GraphQAStreamEvent(
            event="paths",
            data={"items": [item.model_dump(mode="json") for item in response.paths]},
        )
        yield GraphQAStreamEvent(
            event="audit",
            data=response.verification.model_dump(mode="json"),
        )
        midpoint = max(1, len(response.answer) // 2)
        yield GraphQAStreamEvent(
            event="answer_delta",
            data={"delta": response.answer[:midpoint]},
        )
        yield GraphQAStreamEvent(
            event="answer_delta",
            data={"delta": response.answer[midpoint:]},
        )
        yield GraphQAStreamEvent(
            event="done",
            data={"response": response.model_dump(mode="json")},
        )


class DemoLimitStore:
    def __init__(self):
        self.events = {}
        self.inflight = set()

    def ping(self):
        return True

    def consume_rate_limit(self, client_id, request_id):
        events: Deque[str] = self.events.setdefault(client_id, deque())
        events.append(request_id)
        allowed = len(events) <= 60
        return RateLimitDecision(
            allowed=allowed,
            limit=60,
            remaining=max(0, 60 - len(events)),
            retry_after_seconds=0 if allowed else 10,
            reset_after_seconds=60,
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

    def close(self):
        return None


class DemoSessionStore:
    def __init__(self):
        self.sessions = {}
        self.locks = set()

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
        return None


SETTINGS = APISettings(
    auth_required=True,
    api_keys={"browser-fixture": "b" * 32},
    redis_url="redis://fixture:6379/0",
    redis_prefix="graph_qa_browser_fixture",
    rate_limit_requests=60,
    rate_limit_window_seconds=60,
    max_concurrent_requests=4,
    concurrency_lease_seconds=60,
    max_results=50,
    demo_cookie_secure=False,
    demo_trust_proxy_headers=False,
)
LIMIT_STORE = DemoLimitStore()
SESSION_STORE = DemoSessionStore()

app = create_app(
    settings=SETTINGS,
    engine_factory=lambda _settings: DemoEngine(),
    limit_store_factory=lambda _settings: LIMIT_STORE,
    session_store_factory=lambda _settings: SESSION_STORE,
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
