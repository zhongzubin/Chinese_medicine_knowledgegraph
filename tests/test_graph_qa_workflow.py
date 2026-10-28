# -*- coding: utf-8 -*-
"""LangGraph 问答工作流测试，全部使用 fake 外部依赖。"""

from typing import List, Sequence

from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.llm import ReasonerError
from graph_qa.models import (
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
from graph_qa.workflow import GraphQAEngine

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


def make_schema() -> GraphSchema:
    return GraphSchema.from_mapping(
        {
            "format_version": "1.1",
            "node_labels": [
                {"label": label, "properties": [{"name": "name"}]}
                for label in ("Herb", "Formula", "Disease", "Symptom", "Effect", "Source")
            ],
            "relationship_types": [{"type": "HAS_EFFECT"}],
            "triple_patterns": [
                {
                    "subject_label": "Herb",
                    "relationship_type": "HAS_EFFECT",
                    "object_label": "Effect",
                }
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


def make_plan(cypher: str = VALID_QUERY) -> CypherPlan:
    return CypherPlan(
        cypher=cypher,
        parameters={"entity_names": ["麻黄"]},
        used_entities=[UsedEntityRef(label="Herb", name="麻黄")],
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
        self.answer_calls = 0
        self.rewrite_calls = []

    def rewrite_question(self, question, history):
        self.rewrite_calls.append((question, list(history)))
        return RewriteResult(
            standalone_question=self.rewritten_question or question
        )

    def classify_intent(self, question: str) -> IntentResult:
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
    ) -> str:
        self.answer_calls += 1
        return self.grounded_answer

    def stream_answer_from_graph(
        self,
        question,
        linked_entities,
        rows,
        evidence,
        on_token,
    ):
        self.answer_calls += 1
        chunks = [self.grounded_answer[:4], self.grounded_answer[4:]]
        for chunk in chunks:
            if chunk:
                on_token(chunk)
        return "".join(chunks)


class FakeRepository:
    def __init__(self, rows=ROWS):
        self.rows = rows
        self.explain_calls = 0
        self.execute_calls = 0
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
    assert response.query and response.query.cypher == VALID_QUERY
    assert response.evidence[0].object == "发汗"
    assert "不能替代专业医疗诊断和治疗" in response.answer
    assert repository.explain_calls == 1
    assert repository.execute_calls == 1


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

    assert {
        "normalize",
        "rewrite_question",
        "prelink_entities",
        "validate_cypher",
        "answer_from_graph",
    }.issubset(stage_names)
    assert answer_text == done.data["response"]["answer"]
    assert done.data["response"]["grounded"] is True


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

    response = engine.answer("麻黄有什么功效？")

    assert response.intent is not None
    assert response.intent.is_tcm_question is True
    assert response.intent.reason_code == "KNOWN_GRAPH_ENTITY"
    assert response.grounded is True
    assert response.error is None


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

    response = engine.answer("麻黄有什么功效？")

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

    response = engine.answer("麻黄有什么功效？")

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

    response = engine.answer("麻黄有什么功效？")

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


def test_answer_model_failure_uses_evidence_fallback():
    class FailingAnswerReasoner(FakeReasoner):
        def answer_from_graph(self, question, linked_entities, rows, evidence):
            raise RuntimeError("model down")

        def stream_answer_from_graph(
            self,
            question,
            linked_entities,
            rows,
            evidence,
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

    response = engine.answer("麻黄有什么功效？")

    assert response.grounded is True
    assert "麻黄" in response.answer
    assert response.warnings
