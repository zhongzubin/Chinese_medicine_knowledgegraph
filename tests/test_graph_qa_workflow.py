# -*- coding: utf-8 -*-
"""LangGraph 问答工作流测试，全部使用 fake 外部依赖。"""

from typing import List, Sequence

import pytest

from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.llm import ReasonerError
from graph_qa.models import (
    AggregateFact,
    AttributeFact,
    ConversationTurn,
    CypherPlan,
    EntityCandidate,
    ErrorCode,
    Evidence,
    IntentResult,
    LinkedEntity,
    QueryResult,
    RewriteResult,
    UsedEntityRef,
)
from graph_qa.schema import GraphSchema
from graph_qa.workflow import (
    GraphQAEngine,
    _normalize_answer_formatting,
    evidence_from_rows,
    evidence_matches_used_entities,
)

VALID_QUERY = """
MATCH (subject:Herb)-[relationship:HAS_EFFECT]->(object:Effect)
WHERE subject.name IN $entity_names
RETURN subject.name AS subject,
       labels(subject)[0] AS subject_label,
       type(relationship) AS relationship,
       object.name AS object,
       labels(object)[0] AS object_label,
       NULL AS source
LIMIT 20
""".strip()

ROWS = [
    {
        "subject": "麻黄",
        "subject_label": "Herb",
        "relationship": "HAS_EFFECT",
        "object": "发汗",
        "object_label": "Effect",
        "source": None,
    }
]
ATTRIBUTE_ROWS = [
    {
        "subject": "麻黄",
        "subject_label": "Herb",
        "properties": {
            "property_flavor": "辛、微苦，温",
            "meridian": "归肺经、膀胱经",
        },
        "sources": ["《中药大辞典》"],
    }
]
FORMULA_INGREDIENT_ROWS = [
    {
        "subject": "四君子汤",
        "subject_label": "Formula",
        "relationship": "HAS_INGREDIENT",
        "object": "人参",
        "object_label": "Herb",
        "source": None,
    }
]


def make_schema() -> GraphSchema:
    properties = {
        "Herb": [
            "name",
            "property_flavor",
            "meridian",
            "dosage",
            "taboo",
            "usage",
            "effect",
            "indication",
        ],
        "Formula": [
            "name",
            "effect",
            "indication",
            "usage",
            "taboo",
        ],
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
                for label in ("Herb", "Formula", "Disease", "Symptom", "Effect", "Source")
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
            EntityRecord("Herb", "麻黄", "龙沙"),
            EntityRecord("Effect", "发汗"),
        ]
    )


def make_index_with_guizhi() -> EntityIndex:
    return EntityIndex.from_records(
        [
            EntityRecord("Herb", "桂枝"),
            EntityRecord("Effect", "发汗"),
        ]
    )


def make_entity_index_with_effect_name() -> EntityIndex:
    return EntityIndex.from_records([EntityRecord("Effect", "功效")])


def make_formula_index() -> EntityIndex:
    return EntityIndex.from_records(
        [
            EntityRecord("Formula", "四君子汤"),
            EntityRecord("Herb", "人参"),
            EntityRecord("Herb", "白术"),
        ]
    )


def make_plan(
    cypher: str = VALID_QUERY,
    entity_name: str = "麻黄",
) -> CypherPlan:
    return CypherPlan(
        cypher=cypher,
        parameters={"entity_names": [entity_name]},
        used_entities=[UsedEntityRef(label="Herb", name=entity_name)],
        expected_result_shape="herb_effect_pairs",
    )


class FakeReasoner:
    def __init__(
        self,
        intent: IntentResult,
        candidates: Sequence[EntityCandidate] = (),
        plans: Sequence[object] = (),
        direct: str = "直接回答",
        grounded_answer: str = "麻黄具有发汗功效。",
        rewritten_question: str | None = None,
    ):
        self.intent = intent
        self.candidates = list(candidates)
        self.plans = list(plans)
        self.direct = direct
        self.grounded_answer = grounded_answer
        self.rewritten_question = rewritten_question
        self.plan_calls = 0
        self.direct_calls = 0
        self.intent_calls = 0
        self.answer_calls = 0
        self.rewrite_calls = []

    def rewrite_question(self, question, history):
        self.rewrite_calls.append((question, list(history)))
        return RewriteResult(
            standalone_question=self.rewritten_question or question
        )

    def classify_intent(self, question: str) -> IntentResult:
        self.intent_calls += 1
        return self.intent

    def extract_entities(self, question: str) -> List[EntityCandidate]:
        return self.candidates

    def generate_cypher(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        schema: GraphSchema,
        previous_errors: Sequence[str],
    ) -> CypherPlan:
        plan = self.plans[min(self.plan_calls, len(self.plans) - 1)]
        self.plan_calls += 1
        if isinstance(plan, Exception):
            raise plan
        return plan

    def direct_answer(self, question: str) -> str:
        self.direct_calls += 1
        return self.direct

    def stream_direct_answer(self, question, on_token):
        self.direct_calls += 1
        chunks = [self.direct[:3], self.direct[3:]]
        for chunk in chunks:
            if chunk:
                on_token(chunk)
        return "".join(chunks)

    def answer_from_graph(
        self,
        question: str,
        linked_entities: Sequence[LinkedEntity],
        rows: Sequence[dict],
        evidence: Sequence[Evidence],
        facts: Sequence[AttributeFact] = (),
    ) -> str:
        self.answer_calls += 1
        return self.grounded_answer

    def stream_answer_from_graph(
        self,
        question,
        linked_entities,
        rows,
        evidence,
        facts,
        on_token,
    ):
        self.answer_calls += 1
        chunks = [self.grounded_answer[:4], self.grounded_answer[4:]]
        for chunk in chunks:
            if chunk:
                on_token(chunk)
        return "".join(chunks)


class FakeRepository:
    def __init__(
        self,
        rows=ROWS,
        attribute_rows=None,
        aggregate_count=4,
        multi_condition_rows=None,
        multi_hop_rows=None,
    ):
        self.rows = rows
        self.attribute_rows = attribute_rows or []
        self.aggregate_count = aggregate_count
        self.multi_condition_rows = multi_condition_rows or []
        self.multi_hop_rows = multi_hop_rows or []
        self.explain_calls = 0
        self.execute_calls = 0
        self.attribute_calls = 0
        self.aggregate_calls = 0
        self.aggregate_plans = []
        self.multi_condition_calls = 0
        self.multi_hop_calls = 0
        self.closed = False

    def load_entity_index(self):
        return make_index()

    def explain(self, plan):
        self.explain_calls += 1

    def execute_read(self, plan: CypherPlan) -> QueryResult:
        self.execute_calls += 1
        return QueryResult(
            cypher=plan.cypher,
            parameters=plan.parameters,
            rows=list(self.rows),
            row_count=len(self.rows),
            elapsed_ms=1,
            limit_applied=20,
        )

    def execute_attributes(self, plan) -> QueryResult:
        self.attribute_calls += 1
        return QueryResult(
            cypher="MATCH (subject) RETURN properties(subject)",
            parameters={},
            rows=list(self.attribute_rows),
            row_count=len(self.attribute_rows),
            elapsed_ms=1,
            limit_applied=20,
        )

    def execute_aggregate(self, plan) -> QueryResult:
        self.aggregate_calls += 1
        self.aggregate_plans.append(plan)
        names = [item.name for item in plan.used_entities]
        if plan.require_all_entities:
            parameters = {
                f"entity_{index}": name
                for index, name in enumerate(names)
            }
            cypher = (
                "MATCH (subject) WHERE EXISTS { MATCH (subject) } "
                "AND EXISTS { MATCH (subject) } "
                "RETURN count(DISTINCT subject) AS count"
            )
        else:
            result = "object" if plan.direction == "outgoing" else "subject"
            parameters = {"entity_names": names}
            cypher = (
                "MATCH (subject)-[relationship]->(object) "
                f"RETURN count(DISTINCT {result}) AS count"
            )
        return QueryResult(
            cypher=cypher,
            parameters=parameters,
            rows=[{"count": self.aggregate_count}],
            row_count=1,
            elapsed_ms=1,
            limit_applied=1,
        )

    def execute_multi_condition_list(self, plan) -> QueryResult:
        self.multi_condition_calls += 1
        return QueryResult(
            cypher="MATCH (subject) WHERE EXISTS { MATCH (subject) }",
            parameters={
                f"entity_{index}": entity.name
                for index, entity in enumerate(plan.used_entities)
            },
            rows=list(self.multi_condition_rows),
            row_count=len(self.multi_condition_rows),
            elapsed_ms=1,
            limit_applied=20,
        )

    def execute_multi_hop(self, plan) -> QueryResult:
        self.multi_hop_calls += 1
        return QueryResult(
            cypher="MATCH (root)-[first]->(middle)-[second]->(target)",
            parameters={
                "entity_names": [
                    entity.name
                    for entity in plan.used_entities
                ]
            },
            rows=list(self.multi_hop_rows),
            row_count=len(self.multi_hop_rows),
            elapsed_ms=1,
            limit_applied=20,
        )

    def close(self):
        self.closed = True


def tcm_intent() -> IntentResult:
    return IntentResult(
        is_tcm_question=True,
        confidence=0.99,
        matched_domain_terms=["麻黄"],
        reason_code="TCM_ENTITY_PRESENT",
    )


def non_tcm_intent() -> IntentResult:
    return IntentResult(
        is_tcm_question=False,
        confidence=0.99,
        matched_domain_terms=[],
        reason_code="OUT_OF_SCOPE",
    )


def test_workflow_returns_grounded_answer_query_and_evidence():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="麻黄", suggested_label="Herb")],
        plans=[make_plan()],
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄有什么功效？", request_id="test-request")

    assert response.request_id == "test-request"
    assert response.grounded is True
    assert response.error is None
    assert response.linked_entities[0].name == "麻黄"
    assert response.query
    assert "HAS_EFFECT" in response.query.cypher
    assert "$entity_names" in response.query.cypher
    assert response.query.plan_kind == "controlled_relationship"
    assert response.evidence[0].object == "发汗"
    assert response.verification is not None
    assert response.verification.status == "passed"
    assert response.verification.evidence_items == 1
    assert response.verification.missing_sources == 1
    assert "不能替代专业医疗诊断和治疗" in response.answer
    assert repository.explain_calls == 1
    assert repository.execute_calls == 1


def test_attribute_question_uses_controlled_template_and_facts():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        grounded_answer="麻黄的性味是辛、微苦，温；归肺经、膀胱经。",
    )
    repository = FakeRepository(attribute_rows=ATTRIBUTE_ROWS)
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄性味归经是什么？")

    assert response.error is None
    assert response.grounded is True
    assert response.evidence == []
    assert {item.attribute for item in response.facts} == {
        "property_flavor",
        "meridian",
    }
    assert all(item.source == "《中药大辞典》" for item in response.facts)
    assert response.query is not None
    assert response.query.plan_kind == "controlled_attribute"
    assert repository.attribute_calls == 1
    assert repository.execute_calls == 0
    assert reasoner.plan_calls == 0
    assert reasoner.answer_calls == 0


def test_stream_answer_emits_attribute_facts_and_verification_stage():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        grounded_answer="麻黄的性味是辛、微苦，温。",
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(attribute_rows=ATTRIBUTE_ROWS),
        schema=make_schema(),
        entity_index=make_index(),
    )

    events = list(engine.stream_answer("麻黄性味是什么？"))

    fact_event = next(event for event in events if event.event == "facts")
    audit_event = next(event for event in events if event.event == "audit")
    stages = {
        event.data["stage"]
        for event in events
        if event.event == "stage"
    }
    assert fact_event.data["items"][0]["attribute_label"] == "性味"
    assert audit_event.data["status"] == "passed"
    assert audit_event.data["fact_items"] == 1
    assert "verify_answer" in stages


def test_missing_attribute_falls_back_to_relation_query():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        plans=[make_plan()],
    )
    repository = FakeRepository(attribute_rows=[])
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄有什么功效？")

    assert response.error is None
    assert response.grounded is True
    assert response.facts == []
    assert response.evidence[0].object == "发汗"
    assert response.query is not None
    assert "HAS_EFFECT" in response.query.cypher
    assert repository.attribute_calls == 1
    assert repository.execute_calls == 1
    assert reasoner.plan_calls == 0
    assert any("关系回答" in warning for warning in response.warnings)


def test_missing_non_relation_attribute_returns_no_evidence():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository(attribute_rows=[])
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄有什么禁忌？")

    assert response.error is None
    assert response.grounded is False
    assert response.facts == []
    assert "没有找到" in response.answer
    assert response.warnings == [
        "已识别实体，但本轮没有找到可验证的图谱证据。"
    ]
    assert repository.attribute_calls == 1
    assert repository.execute_calls == 0
    assert reasoner.plan_calls == 0


def test_formula_ingredient_question_uses_relationship_template():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        grounded_answer="四君子汤含有的人参可由当前图谱关系确认。",
    )
    repository = FakeRepository(rows=FORMULA_INGREDIENT_ROWS)
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("四君子汤由哪些药材组成？")

    assert response.error is None
    assert response.grounded is True
    assert response.evidence[0].object == "人参"
    assert response.query is not None
    assert "HAS_INGREDIENT" in response.query.cypher
    assert response.query.plan_kind == "controlled_relationship"
    assert reasoner.plan_calls == 0
    assert repository.execute_calls == 1


def test_reverse_formula_lookup_uses_relationship_template():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        grounded_answer="四君子汤含有人参。",
    )
    repository = FakeRepository(rows=FORMULA_INGREDIENT_ROWS)
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("哪些方剂含有人参？")

    assert response.error is None
    assert response.grounded is True
    assert response.query is not None
    assert "object.name IN $entity_names" in response.query.cypher
    assert response.query.plan_kind == "controlled_relationship"
    assert reasoner.plan_calls == 0


def test_formula_ingredient_count_uses_aggregate_template():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository(aggregate_count=4)
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("四君子汤由多少味药材组成？")

    assert response.error is None
    assert response.grounded is True
    assert response.aggregates == [
        AggregateFact(
            anchor="四君子汤",
            anchor_label="Formula",
            relationship="HAS_INGREDIENT",
            result_label="Herb",
            direction="outgoing",
            count=4,
            description="四君子汤关联的药材",
        )
    ]
    assert "4 个" in response.answer
    assert reasoner.plan_calls == 0
    assert reasoner.answer_calls == 0
    assert repository.aggregate_calls == 1


def test_reverse_aggregate_counts_formulas_containing_herb():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository(aggregate_count=7)
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("含有人参的方剂有多少种？")

    assert response.grounded is True
    assert response.aggregates[0].direction == "incoming"
    assert response.aggregates[0].result_label == "Formula"
    assert response.aggregates[0].count == 7
    assert response.query is not None
    assert "count(DISTINCT subject)" in response.query.cypher
    assert response.query.plan_kind == "controlled_aggregate"
    assert reasoner.plan_calls == 0


def test_multi_condition_aggregate_requires_both_herbs():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository(aggregate_count=7)
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("同时含有人参和白术的方剂有多少种？")

    assert response.grounded is True
    assert response.aggregates[0].count == 7
    assert response.aggregates[0].description == "同时关联到人参、白术的方剂"
    assert response.query.plan_kind == "controlled_aggregate"
    assert repository.aggregate_plans[0].require_all_entities is True
    assert reasoner.plan_calls == 0


def test_multi_condition_list_returns_matching_formulas_and_evidence():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository(
        multi_condition_rows=[
            {
                "subject": "四君子汤",
                "subject_label": "Formula",
                "conditions": [
                    {
                        "relationship": "HAS_INGREDIENT",
                        "object": "人参",
                        "object_label": "Herb",
                        "sources": [],
                    },
                    {
                        "relationship": "HAS_INGREDIENT",
                        "object": "白术",
                        "object_label": "Herb",
                        "sources": [],
                    },
                ],
            }
        ]
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("同时含有人参和白术的方剂有哪些？")

    assert response.grounded is True
    assert response.query.plan_kind == "controlled_multi_condition_list"
    assert response.answer.startswith(
        "根据当前图谱，同时关联到人参、白术的方剂如下："
    )
    assert "四君子汤" in response.answer
    assert {(item.object) for item in response.evidence} == {
        "人参",
        "白术",
    }
    assert repository.multi_condition_calls == 1
    assert reasoner.plan_calls == 0
    assert reasoner.answer_calls == 0


def test_multi_hop_question_returns_paths_and_deterministic_answer():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository(
        multi_hop_rows=[
            {
                "root": "四君子汤",
                "root_label": "Formula",
                "first_relationship": "HAS_INGREDIENT",
                "intermediate": "人参",
                "intermediate_label": "Herb",
                "second_relationship": "HAS_EFFECT",
                "target": "补气",
                "target_label": "Effect",
                "sources": [],
            }
        ]
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    response = engine.answer("四君子汤所含药材有什么功效？")

    assert response.grounded is True
    assert response.error is None
    assert response.query.plan_kind == "controlled_multi_hop"
    assert len(response.paths) == 1
    assert response.paths[0].intermediate == "人参"
    assert response.paths[0].target == "补气"
    assert response.verification is not None
    assert response.verification.path_items == 1
    assert response.verification.status == "passed"
    assert "人参：补气" in response.answer
    assert repository.multi_hop_calls == 1
    assert reasoner.plan_calls == 0
    assert reasoner.answer_calls == 0


def test_stream_answer_emits_aggregate_event_and_count():
    engine = GraphQAEngine(
        reasoner=FakeReasoner(intent=tcm_intent()),
        repository=FakeRepository(aggregate_count=4),
        schema=make_schema(),
        entity_index=make_formula_index(),
    )

    events = list(engine.stream_answer("四君子汤由多少味药材组成？"))

    aggregate_event = next(
        event for event in events if event.event == "aggregates"
    )
    answer_text = "".join(
        event.data["delta"]
        for event in events
        if event.event == "answer_delta"
    )
    assert aggregate_event.data["items"][0]["count"] == 4
    assert "4 个" in answer_text


def test_entity_source_question_uses_forward_relationship_direction():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        grounded_answer="大枣出处为《本草纲目》。",
    )
    repository = FakeRepository(
        rows=[
            {
                "subject": "大枣",
                "subject_label": "Herb",
                "relationship": "FROM_SOURCE",
                "object": "《本草纲目》",
                "object_label": "Source",
                "source": None,
            }
        ]
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=EntityIndex.from_records(
            [EntityRecord("Herb", "大枣")]
        ),
    )

    response = engine.answer("大枣的出处有哪些？")

    assert response.grounded is True
    assert response.query is not None
    assert "MATCH (subject:Herb)-[relationship:FROM_SOURCE]" in response.query.cypher
    assert "OPTIONAL MATCH" not in response.query.cypher
    assert reasoner.plan_calls == 0


def test_conversation_history_rewrites_pronoun_before_entity_linking():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        plans=[make_plan()],
        rewritten_question="麻黄有什么功效？",
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )
    history = [
        ConversationTurn(
            question="麻黄是什么？",
            answer="麻黄是一味中药材。",
            linked_entities=[
                LinkedEntity(
                    label="Herb",
                    name="麻黄",
                    matched_text="麻黄",
                    match_type="name",
                    score=1.0,
                )
            ],
            grounded=True,
        )
    ]

    response = engine.answer(
        "它有什么功效？",
        conversation_history=history,
        session_id="session-1",
    )

    assert response.rewritten_question == "麻黄有什么功效?"
    assert response.session_id == "session-1"
    assert response.grounded is True
    assert reasoner.rewrite_calls[0][1] == history


def test_explicit_new_entity_skips_context_rewrite():
    reasoner = FakeReasoner(intent=tcm_intent())
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=EntityIndex.from_records(
            [
                EntityRecord("Herb", "麻黄"),
                EntityRecord("Herb", "桂枝"),
            ]
        ),
    )
    history = [
        ConversationTurn(
            question="麻黄有什么功效？",
            answer="麻黄具有发汗功效。",
            linked_entities=[
                LinkedEntity(
                    label="Herb",
                    name="麻黄",
                    matched_text="麻黄",
                    match_type="name",
                    score=1.0,
                )
            ],
            grounded=True,
        )
    ]

    update = engine._rewrite_question(
        {
            "normalized_question": "桂枝有什么功效？",
            "conversation_history": history,
        }
    )

    assert update["rewritten_question"] == "桂枝有什么功效？"
    assert reasoner.rewrite_calls == []


def test_rewrite_failure_uses_recent_entity_and_pronoun():
    class FailingRewriteReasoner(FakeReasoner):
        def rewrite_question(self, question, history):
            raise RuntimeError("rewrite unavailable")

    reasoner = FailingRewriteReasoner(intent=tcm_intent())
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )
    history = [
        ConversationTurn(
            question="麻黄有什么功效？",
            answer="麻黄具有发汗功效。",
            linked_entities=[
                LinkedEntity(
                    label="Herb",
                    name="麻黄",
                    matched_text="麻黄",
                    match_type="name",
                    score=1.0,
                )
            ],
            grounded=True,
        )
    ]

    update = engine._rewrite_question(
        {
            "normalized_question": "它有什么禁忌？",
            "conversation_history": history,
        }
    )

    assert update["rewritten_question"] == "麻黄有什么禁忌?"
    assert update["warnings"]


def test_pronoun_after_entity_clarification_requires_new_context():
    reasoner = FakeReasoner(intent=tcm_intent(), rewritten_question="麻黄归经是什么？")
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )
    history = [
        ConversationTurn(
            question="星辰草有什么功效？",
            answer="请补充具体实体。",
            linked_entities=[],
            grounded=False,
        )
    ]

    update = engine._rewrite_question(
        {
            "normalized_question": "它归经是什么？",
            "conversation_history": history,
        }
    )

    assert update["rewritten_question"] == "它归经是什么？"
    assert update["warnings"]
    assert reasoner.rewrite_calls == []


def test_stream_answer_emits_public_stages_progress_and_answer_deltas():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        plans=[make_plan()],
        grounded_answer="麻黄具有发汗功效。",
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )

    events = list(engine.stream_answer("麻黄有什么功效？", request_id="stream-1"))

    stage_names = {
        event.data["stage"]
        for event in events
        if event.event == "stage"
    }
    answer_text = "".join(
        event.data["delta"]
        for event in events
        if event.event == "answer_delta"
    )
    done = next(event for event in events if event.event == "done")
    audit = next(event for event in events if event.event == "audit")
    plan_stages = [
        event.data
        for event in events
        if (
            event.event == "stage"
            and event.data.get("stage") == "plan_cypher"
        )
    ]
    observe_stages = [
        event.data
        for event in events
        if (
            event.event == "stage"
            and event.data.get("stage") == "retrieve_observe"
        )
    ]

    assert {
        "normalize",
        "rewrite_question",
        "prelink_entities",
        "validate_cypher",
        "answer_from_graph",
    }.issubset(stage_names)
    assert answer_text == done.data["response"]["answer"]
    assert done.data["response"]["grounded"] is True
    assert done.data["response"]["verification"]["status"] == "passed"
    assert audit.data["agent"] == "evidence_audit"
    assert [
        item["plan_kind"]
        for item in plan_stages
    ] == [
        "controlled_attribute",
        "controlled_relationship",
    ]
    assert [
        item["plan_label"]
        for item in plan_stages
    ] == ["图谱属性", "受控关系"]
    assert all(
        "cypher" not in item and "parameters" not in item
        for item in plan_stages
    )
    assert [
        item["decision"]
        for item in observe_stages
    ] == ["replan", "answer_from_graph"]
    assert [
        item["reason_code"]
        for item in observe_stages
    ] == [
        "ATTRIBUTE_EMPTY_RELATION_FALLBACK",
        "VERIFIABLE_CONTEXT_AVAILABLE",
    ]


def test_unsupported_answer_is_replaced_by_evidence_fallback():
    engine = GraphQAEngine(
        reasoner=FakeReasoner(intent=tcm_intent()),
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )

    update = engine._verify_answer(
        {
            "answer": "麻黄可以治疗任何疾病。",
            "evidence": [
                Evidence(
                    subject="麻黄",
                    subject_label="Herb",
                    relationship="HAS_EFFECT",
                    object="发汗",
                    object_label="Effect",
                )
            ],
            "facts": [],
        }
    )

    assert "发汗" in update["answer"]
    assert update["grounded"] is True
    assert update["warnings"]
    assert update["verification"].status == "fallback"
    assert update["verification"].fallback_used is True


def test_answer_formatting_splits_inline_markdown_bullets():
    facts = [
        AttributeFact(
            subject="麻黄",
            subject_label="Herb",
            attribute="property_flavor",
            attribute_label="性味",
            value="辛、微苦，温",
        ),
        AttributeFact(
            subject="麻黄",
            subject_label="Herb",
            attribute="meridian",
            attribute_label="归经",
            value="肺经",
        ),
    ]

    assert _normalize_answer_formatting(
        "属性如下：-**性味**：辛、微苦，温-归经：肺经",
        facts,
    ) == "属性如下：\n性味：辛、微苦，温\n归经：肺经"


def test_non_tcm_question_uses_direct_answer_without_graph_query():
    reasoner = FakeReasoner(
        intent=non_tcm_intent(),
        direct="量子计算使用量子比特。",
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("量子计算是什么？")

    assert response.grounded is False
    assert response.query is None
    assert response.linked_entities == []
    assert response.verification is None
    assert any("未使用知识图谱证据" in warning for warning in response.warnings)
    assert "不能替代专业医疗诊断和治疗" not in response.answer
    assert repository.execute_calls == 0
    assert reasoner.direct_calls == 1


def test_known_graph_entity_overrides_incorrect_model_intent():
    reasoner = FakeReasoner(
        intent=non_tcm_intent(),
        candidates=[],
        plans=[make_plan()],
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄能做什么？")

    assert response.intent is not None
    assert response.intent.is_tcm_question is True
    assert response.intent.reason_code == "KNOWN_GRAPH_ENTITY"
    assert response.grounded is True
    assert response.error is None


def test_prelink_uses_exact_matches_only():
    class FailingSemanticMatcher:
        def match(self, text):
            raise AssertionError(f"预匹配不应调用语义召回: {text}")

    entity_index = EntityIndex.from_records(
        [EntityRecord("Herb", "麻黄")],
        semantic_index=FailingSemanticMatcher(),
    )
    engine = GraphQAEngine(
        reasoner=FakeReasoner(intent=tcm_intent()),
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=entity_index,
    )

    update = engine._prelink_entities({"normalized_question": "近似药材有什么作用？"})

    assert update["prelinked_entities"] == []


def test_unresolved_pronoun_does_not_enter_semantic_entity_linking():
    class FailingSemanticMatcher:
        def match(self, text):
            raise AssertionError(f"未解析代词不应调用语义匹配: {text}")

    entity_index = EntityIndex.from_records(
        [EntityRecord("Herb", "麻黄")],
        semantic_index=FailingSemanticMatcher(),
    )
    engine = GraphQAEngine(
        reasoner=FakeReasoner(intent=tcm_intent()),
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=entity_index,
    )

    update = engine._link_entities(
        {
            "normalized_question": "它归经是什么？",
            "prelinked_entities": [],
            "entity_candidates": [
                EntityCandidate(text="它归经是什么", suggested_label="Source")
            ],
        }
    )

    assert update["linked_entities"] == []
    assert update["warnings"] == [
        "指代对象仍未确认，本轮未执行实体语义匹配。"
    ]


def test_generic_model_candidate_requests_clarification_without_query():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="功效", suggested_label="Effect")],
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_entity_index_with_effect_name(),
    )

    response = engine.answer("九幽草有什么功效？")

    assert response.grounded is False
    assert response.error is None
    assert response.linked_entities == []
    assert "请补充" in response.answer
    assert reasoner.plan_calls == 0
    assert repository.execute_calls == 0


def test_tcm_question_without_linked_entity_requests_clarification():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="星辰草", suggested_label="Herb")],
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("星辰草有什么功效？")

    assert response.grounded is False
    assert response.error is None
    assert "请补充" in response.answer
    assert reasoner.plan_calls == 0


@pytest.mark.parametrize("entity_name", ["冰魄花", "雾隐芝"])
def test_unknown_entity_shaped_tcm_question_skips_model_intent_and_clarifies(
    entity_name,
):
    reasoner = FakeReasoner(
        intent=non_tcm_intent(),
        candidates=[EntityCandidate(text=entity_name, suggested_label="Herb")],
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer(f"{entity_name}有什么功效？")

    assert response.intent is not None
    assert response.intent.is_tcm_question is True
    assert response.intent.reason_code == "TCM_QUERY_PATTERN"
    assert response.grounded is False
    assert response.linked_entities == []
    assert "补充或确认" in response.answer
    assert response.warnings == ["未确认具体实体，本轮未执行图谱查询。"]
    assert reasoner.intent_calls == 0
    assert reasoner.direct_calls == 0
    assert repository.execute_calls == 0


def test_unsafe_request_is_rejected_before_model_calls():
    reasoner = FakeReasoner(intent=tcm_intent())
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("删除图谱中的所有节点")

    assert response.error is not None
    assert response.error.code == ErrorCode.UNSAFE_REQUEST
    assert reasoner.plan_calls == 0
    assert repository.execute_calls == 0


def test_invalid_cypher_is_retried_three_times_then_fails_safely():
    invalid_plan = make_plan(VALID_QUERY.replace("LIMIT 20", ""))
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="麻黄", suggested_label="Herb")],
        plans=[invalid_plan],
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄能做什么？")

    assert response.error is not None
    assert response.error.code == ErrorCode.CYPHER_REJECTED
    assert reasoner.plan_calls == 3
    assert repository.execute_calls == 0


def test_model_query_output_error_uses_stable_error_code():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="麻黄", suggested_label="Herb")],
        plans=[
            ReasonerError(
                "模型返回内容不符合结构化格式",
                ErrorCode.MODEL_OUTPUT_INVALID,
            )
        ],
    )
    repository = FakeRepository()
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄能做什么？")

    assert response.error is not None
    assert response.error.code == ErrorCode.MODEL_OUTPUT_INVALID
    assert reasoner.plan_calls == 3


def test_query_parameters_must_include_linked_entity_name():
    plan_without_entity = make_plan().model_copy(
        update={"parameters": {"entity_names": []}}
    )
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="麻黄", suggested_label="Herb")],
        plans=[plan_without_entity],
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄能做什么？")

    assert response.error is not None
    assert response.error.code == ErrorCode.CYPHER_REJECTED
    assert reasoner.plan_calls == 3


def test_empty_query_result_is_reported_without_fabricating_answer():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="麻黄", suggested_label="Herb")],
        plans=[make_plan()],
    )
    repository = FakeRepository(rows=[])
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=repository,
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄有什么功效？")

    assert response.grounded is False
    assert response.evidence == []
    assert "没有找到" in response.answer
    assert reasoner.answer_calls == 0


def test_evidence_must_match_a_used_entity():
    refs = [
        UsedEntityRef(label="Disease", name="感冒"),
        UsedEntityRef(label="Formula", name="银翘散"),
    ]
    evidence = [
        Evidence(
            subject="银翘散",
            subject_label="Formula",
            relationship="HAS_EFFECT",
            object="清热解毒",
            object_label="Effect",
        ),
        Evidence(
            subject="银翘散",
            subject_label="Formula",
            relationship="TREATS_DISEASE",
            object="感冒",
            object_label="Disease",
        ),
    ]

    assert evidence_matches_used_entities(evidence, refs) is True
    assert evidence_matches_used_entities(evidence[:1], refs) is True
    assert evidence_matches_used_entities(
        [
            Evidence(
                subject="麻黄",
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object="发汗",
                object_label="Effect",
            )
        ],
        refs,
    ) is False


def test_evidence_merges_multiple_sources_for_the_same_relation():
    rows = [
        {
            **ROWS[0],
            "source": "《局方》",
        },
        {
            **ROWS[0],
            "source": "《宋·太平惠民和剂局方》",
        },
    ]

    evidence = evidence_from_rows(rows, make_schema())

    assert len(evidence) == 1
    assert evidence[0].source == "《局方》；《宋·太平惠民和剂局方》"


def test_unrelated_evidence_does_not_mark_answer_grounded():
    reasoner = FakeReasoner(
        intent=tcm_intent(),
        plans=[make_plan(entity_name="桂枝")],
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(rows=ROWS),
        schema=make_schema(),
        entity_index=make_index_with_guizhi(),
    )

    response = engine.answer("桂枝有什么功效？")

    assert response.grounded is False
    assert response.evidence == []
    assert response.error is not None
    assert response.error.code == ErrorCode.NO_GRAPH_EVIDENCE
    assert "没有找到" in response.answer
    assert reasoner.answer_calls == 0


def test_answer_model_failure_uses_evidence_fallback():
    class FailingAnswerReasoner(FakeReasoner):
        def answer_from_graph(
            self,
            question,
            linked_entities,
            rows,
            evidence,
            facts=(),
        ):
            raise RuntimeError("model down")

        def stream_answer_from_graph(
            self,
            question,
            linked_entities,
            rows,
            evidence,
            facts,
            on_token,
        ):
            raise RuntimeError("model down")

    reasoner = FailingAnswerReasoner(
        intent=tcm_intent(),
        candidates=[EntityCandidate(text="麻黄", suggested_label="Herb")],
        plans=[make_plan()],
    )
    engine = GraphQAEngine(
        reasoner=reasoner,
        repository=FakeRepository(),
        schema=make_schema(),
        entity_index=make_index(),
    )

    response = engine.answer("麻黄能做什么？")

    assert response.grounded is True
    assert "麻黄" in response.answer
    assert response.warnings
