# -*- coding: utf-8 -*-
"""问答角色 Agent 的注册与工作流映射测试。"""

import pytest

from graph_qa.agents import (
    AgentDescriptor,
    AgentRegistry,
    EvidenceAuditAgent,
    QAAgentTeam,
    RetrievalAgent,
    RetrievalLoopObservation,
)
from graph_qa.agents.retrieval import MULTI_HOP_CATALOG
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.llm import ReasonerError
from graph_qa.models import (
    AggregateQueryPlan,
    AttributeFact,
    AttributeQueryPlan,
    CypherPlan,
    ErrorCode,
    Evidence,
    LinkedEntity,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    PathEvidence,
    RetrievalPlanningInput,
    RetrievalPlanningResult,
    UsedEntityRef,
)
from graph_qa.schema import GraphSchema
from graph_qa.workflow import GraphQAEngine, STAGE_LABELS


EXPECTED_STAGE_AGENTS = {
    "normalize": "dialogue",
    "rewrite_question": "dialogue",
    "prelink_entities": "intent",
    "classify_intent": "intent",
    "extract_entities": "entity",
    "link_entities": "entity",
    "plan_cypher": "retrieval",
    "validate_cypher": "retrieval",
    "execute_cypher": "retrieval",
    "retrieve_observe": "retrieval",
    "answer_from_graph": "answer",
    "direct_answer": "answer",
    "verify_answer": "evidence_audit",
    "clarify": "evidence_audit",
    "no_evidence": "evidence_audit",
    "safe_fail": "evidence_audit",
    "finish": "evidence_audit",
}
TRACE_STAGES = (
    "normalize",
    "rewrite_question",
    "prelink_entities",
    "classify_intent",
    "extract_entities",
    "link_entities",
    "plan_cypher",
    "validate_cypher",
    "execute_cypher",
    "retrieve_observe",
    "answer_from_graph",
    "verify_answer",
    "finish",
)


def make_schema() -> GraphSchema:
    properties = {
        "Herb": [
            "name",
            "property_flavor",
            "meridian",
            "effect",
            "taboo",
            "usage",
        ],
        "Formula": [
            "name",
            "effect",
            "indication",
            "usage",
            "taboo",
            "ingredients",
            "alias",
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
                {"type": "TREATS_DISEASE"},
                {"type": "ALLEVIATES_SYMPTOM"},
                {"type": "HAS_SYMPTOM"},
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
                    "subject_label": "Formula",
                    "relationship_type": "HAS_EFFECT",
                    "object_label": "Effect",
                },
                {
                    "subject_label": "Herb",
                    "relationship_type": "TREATS_DISEASE",
                    "object_label": "Disease",
                },
                {
                    "subject_label": "Herb",
                    "relationship_type": "ALLEVIATES_SYMPTOM",
                    "object_label": "Symptom",
                },
                {
                    "subject_label": "Disease",
                    "relationship_type": "HAS_SYMPTOM",
                    "object_label": "Symptom",
                },
                {
                    "subject_label": "Formula",
                    "relationship_type": "FROM_SOURCE",
                    "object_label": "Source",
                },
            ],
        }
    )


class FakeStageGraph:
    def stream(self, initial_state, stream_mode):
        del stream_mode
        for stage in TRACE_STAGES:
            yield "updates", {
                stage: {
                    "stage_events": [stage],
                }
            }
        yield "values", {
            **initial_state,
            "answer": "测试回答",
            "grounded": True,
        }


def linked_entity(label: str, name: str) -> LinkedEntity:
    return LinkedEntity(
        label=label,
        name=name,
        matched_text=name,
        match_type="name",
        score=1.0,
    )


def make_cypher_plan(entity_name: str = "麻黄") -> CypherPlan:
    return CypherPlan(
        cypher=(
            "MATCH (subject:Herb)-[relationship:HAS_EFFECT]->(object:Effect) "
            "WHERE subject.name IN $entity_names "
            "RETURN subject.name AS subject, "
            "labels(subject)[0] AS subject_label, "
            "type(relationship) AS relationship, "
            "object.name AS object, "
            "labels(object)[0] AS object_label, "
            "NULL AS source LIMIT 20"
        ),
        parameters={"entity_names": [entity_name]},
        used_entities=[UsedEntityRef(label="Herb", name=entity_name)],
        expected_result_shape="herb_effect_pairs",
    )


class PlanReasoner:
    def __init__(self, plan=None, error=None):
        self.plan = plan
        self.error = error
        self.calls = []

    def generate_cypher(self, question, linked_entities, schema, errors):
        self.calls.append(
            (question, list(linked_entities), schema, list(errors))
        )
        if self.error is not None:
            raise self.error
        return self.plan


@pytest.mark.parametrize(
    ("question", "entities", "expected_kind", "expected_type"),
    [
        (
            "四君子汤所含药材有什么功效？",
            [linked_entity("Formula", "四君子汤")],
            "controlled_multi_hop",
            MultiHopQueryPlan,
        ),
        (
            "麻黄性味归经是什么？",
            [linked_entity("Herb", "麻黄")],
            "controlled_attribute",
            AttributeQueryPlan,
        ),
        (
            "含有人参的方剂有多少种？",
            [linked_entity("Herb", "人参")],
            "controlled_aggregate",
            AggregateQueryPlan,
        ),
        (
            "同时含有人参和白术的方剂有哪些？",
            [
                linked_entity("Herb", "人参"),
                linked_entity("Herb", "白术"),
            ],
            "controlled_multi_condition_list",
            MultiConditionQueryPlan,
        ),
        (
            "哪些方剂含有人参？",
            [linked_entity("Herb", "人参")],
            "controlled_relationship",
            CypherPlan,
        ),
    ],
)
def test_retrieval_agent_selects_controlled_plan_without_model_fallback(
    question,
    entities,
    expected_kind,
    expected_type,
):
    reasoner = PlanReasoner()
    agent = RetrievalAgent(
        reasoner=reasoner,
        schema=make_schema(),
        max_results=50,
    )

    result = agent.plan(
        RetrievalPlanningInput(
            question=question,
            linked_entities=entities,
        )
    )

    assert result.plan_kind == expected_kind
    assert isinstance(result.plan, expected_type)
    assert result.query_attempts == 0
    assert reasoner.calls == []


def test_retrieval_agent_uses_schema_constrained_model_fallback():
    reasoner = PlanReasoner(plan=make_cypher_plan())
    agent = RetrievalAgent(
        reasoner=reasoner,
        schema=make_schema(),
        max_results=50,
    )

    result = agent.plan(
        RetrievalPlanningInput(
            question="麻黄能做什么？",
            linked_entities=[linked_entity("Herb", "麻黄")],
            validation_errors=["上一次查询缺少 LIMIT"],
            query_attempts=1,
        )
    )

    assert result.plan_kind == "schema_constrained_cypher"
    assert isinstance(result.plan, CypherPlan)
    assert result.query_attempts == 2
    assert reasoner.calls[0][3] == ["上一次查询缺少 LIMIT"]


def test_retrieval_agent_rejects_model_plan_outside_linked_entities():
    agent = RetrievalAgent(
        reasoner=PlanReasoner(plan=make_cypher_plan("桂枝")),
        schema=make_schema(),
        max_results=50,
    )

    result = agent.plan(
        RetrievalPlanningInput(
            question="麻黄能做什么？",
            linked_entities=[linked_entity("Herb", "麻黄")],
        )
    )

    assert result.plan_kind == "rejected"
    assert result.plan is None
    assert result.query_attempts == 1
    assert result.last_error_code == ErrorCode.CYPHER_REJECTED
    assert result.validation_errors == ["模型引用了未链接的实体"]


def test_retrieval_agent_rejects_model_plan_without_linked_entity_parameter():
    plan = make_cypher_plan().model_copy(
        update={"parameters": {"entity_names": []}}
    )
    agent = RetrievalAgent(
        reasoner=PlanReasoner(plan=plan),
        schema=make_schema(),
        max_results=50,
    )

    result = agent.plan(
        RetrievalPlanningInput(
            question="麻黄能做什么？",
            linked_entities=[linked_entity("Herb", "麻黄")],
        )
    )

    assert result.plan_kind == "rejected"
    assert result.last_error_code == ErrorCode.CYPHER_REJECTED
    assert result.validation_errors == ["查询参数没有包含已链接实体名称"]


def test_retrieval_agent_preserves_model_error_code_without_execution():
    error = ReasonerError("模型输出格式错误", ErrorCode.MODEL_OUTPUT_INVALID)
    agent = RetrievalAgent(
        reasoner=PlanReasoner(error=error),
        schema=make_schema(),
        max_results=50,
    )

    result = agent.plan(
        RetrievalPlanningInput(
            question="麻黄能做什么？",
            linked_entities=[linked_entity("Herb", "麻黄")],
        )
    )

    assert result.plan_kind == "rejected"
    assert result.last_error_code == ErrorCode.MODEL_OUTPUT_INVALID
    assert result.validation_errors == ["模型输出格式错误"]
    assert not hasattr(agent, "repository")
    assert not hasattr(agent, "guard")


def test_retrieval_plan_result_requires_matching_payload():
    with pytest.raises(ValueError, match="必须携带 AttributeQueryPlan"):
        RetrievalPlanningResult(
            plan_kind="controlled_attribute",
            plan=AggregateQueryPlan(
                subject_label="Formula",
                relationship="HAS_INGREDIENT",
                object_label="Herb",
                used_entities=[
                    UsedEntityRef(label="Formula", name="四君子汤")
                ],
                direction="outgoing",
                description="四君子汤关联的药材",
            ),
        )


def test_retrieval_multi_hop_catalog_is_fixed_and_contiguous():
    expected_patterns = {
        (
            ("Formula", "HAS_INGREDIENT", "Herb"),
            ("Herb", "HAS_EFFECT", "Effect"),
        ),
        (
            ("Formula", "HAS_INGREDIENT", "Herb"),
            ("Herb", "TREATS_DISEASE", "Disease"),
        ),
        (
            ("Formula", "HAS_INGREDIENT", "Herb"),
            ("Herb", "ALLEVIATES_SYMPTOM", "Symptom"),
        ),
        (
            ("Herb", "TREATS_DISEASE", "Disease"),
            ("Disease", "HAS_SYMPTOM", "Symptom"),
        ),
    }

    assert {
        (first_step, second_step)
        for first_step, second_step, _description in MULTI_HOP_CATALOG
    } == expected_patterns
    assert all(
        first_step[2] == second_step[0]
        for first_step, second_step, _description in MULTI_HOP_CATALOG
    )


@pytest.mark.parametrize(
    ("observation", "next_action", "reason_code"),
    [
        (
            RetrievalLoopObservation(
                step=1,
                max_steps=3,
                plan_kind="schema_constrained_cypher",
                is_valid=False,
                error_code=None,
                attribute_relation_fallback=False,
                has_context=False,
                row_count=0,
            ),
            "replan",
            "VALIDATION_RETRY",
        ),
        (
            RetrievalLoopObservation(
                step=3,
                max_steps=3,
                plan_kind="schema_constrained_cypher",
                is_valid=False,
                error_code=None,
                attribute_relation_fallback=False,
                has_context=False,
                row_count=0,
            ),
            "safe_fail",
            "VALIDATION_RETRIES_EXHAUSTED",
        ),
        (
            RetrievalLoopObservation(
                step=1,
                max_steps=3,
                plan_kind="controlled_attribute",
                is_valid=True,
                error_code=None,
                attribute_relation_fallback=True,
                has_context=False,
                row_count=1,
            ),
            "replan",
            "ATTRIBUTE_EMPTY_RELATION_FALLBACK",
        ),
        (
            RetrievalLoopObservation(
                step=1,
                max_steps=3,
                plan_kind="controlled_relationship",
                is_valid=True,
                error_code=None,
                attribute_relation_fallback=False,
                has_context=True,
                row_count=3,
            ),
            "answer_from_graph",
            "VERIFIABLE_CONTEXT_AVAILABLE",
        ),
        (
            RetrievalLoopObservation(
                step=1,
                max_steps=3,
                plan_kind="controlled_relationship",
                is_valid=True,
                error_code=None,
                attribute_relation_fallback=False,
                has_context=False,
                row_count=0,
            ),
            "no_evidence",
            "QUERY_RETURNED_NO_ROWS",
        ),
        (
            RetrievalLoopObservation(
                step=1,
                max_steps=3,
                plan_kind="controlled_relationship",
                is_valid=True,
                error_code="NO_GRAPH_EVIDENCE",
                attribute_relation_fallback=False,
                has_context=False,
                row_count=1,
            ),
            "finish",
            "EXECUTION_ERROR",
        ),
    ],
)
def test_retrieval_agent_observes_tool_result_and_decides_next_action(
    observation,
    next_action,
    reason_code,
):
    agent = RetrievalAgent(
        reasoner=object(),
        schema=make_schema(),
        max_results=50,
    )

    decision = agent.observe(observation)

    assert decision.next_action == next_action
    assert decision.reason_code == reason_code


def test_agent_registry_has_unique_names_and_stage_owners():
    team = QAAgentTeam(reasoner=object(), schema=make_schema())
    descriptors = team.registry.descriptors

    names = [descriptor.name for descriptor in descriptors]
    stages = [
        stage
        for descriptor in descriptors
        for stage in descriptor.stages
    ]

    assert len(names) == len(set(names))
    assert len(stages) == len(set(stages))
    assert set(names) == {
        "dialogue",
        "intent",
        "entity",
        "retrieval",
        "answer",
        "evidence_audit",
    }


def test_every_public_workflow_stage_maps_to_expected_agent():
    team = QAAgentTeam(reasoner=object(), schema=make_schema())

    assert set(STAGE_LABELS) == set(EXPECTED_STAGE_AGENTS)
    assert {
        stage: team.agent_for_stage(stage).name
        for stage in STAGE_LABELS
    } == EXPECTED_STAGE_AGENTS


def test_agent_registry_rejects_duplicate_names_and_stages():
    with pytest.raises(ValueError, match="重复的 Agent 名称: duplicate"):
        AgentRegistry(
            [
                AgentDescriptor("duplicate", "first", ("stage_a",)),
                AgentDescriptor("duplicate", "second", ("stage_b",)),
            ]
        )

    with pytest.raises(ValueError, match="阶段被多个 Agent 注册: shared"):
        AgentRegistry(
            [
                AgentDescriptor("first", "first", ("shared",)),
                AgentDescriptor("second", "second", ("shared",)),
            ]
        )


def test_agent_team_reports_unknown_agent_and_stage():
    team = QAAgentTeam(reasoner=object(), schema=make_schema())

    with pytest.raises(KeyError, match="未知 Agent: missing"):
        team.registry.get("missing")
    with pytest.raises(KeyError, match="阶段未注册 Agent: missing"):
        team.agent_for_stage("missing")


def test_stream_stage_events_include_mapped_agent_names():
    engine = GraphQAEngine(
        reasoner=object(),
        repository=object(),
        schema=make_schema(),
        entity_index=EntityIndex.from_records(
            [EntityRecord("Herb", "麻黄")]
        ),
    )
    engine.graph = FakeStageGraph()

    events = list(engine.stream_answer("麻黄有什么功效？"))
    stage_agents = {
        event.data["stage"]: event.data["agent"]
        for event in events
        if event.event == "stage"
    }

    assert stage_agents == {
        stage: EXPECTED_STAGE_AGENTS[stage]
        for stage in TRACE_STAGES
    }


def test_evidence_audit_agent_passes_supported_answer_and_counts_sources():
    agent = EvidenceAuditAgent()

    decision = agent.audit(
        "麻黄具有发汗功效，性味为辛、微苦，温。",
        evidence=[
            Evidence(
                subject="麻黄",
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object="发汗",
                object_label="Effect",
            )
        ],
        facts=[
            AttributeFact(
                subject="麻黄",
                subject_label="Herb",
                attribute="property_flavor",
                attribute_label="性味",
                value="辛、微苦，温",
                source="《中药大辞典》",
            )
        ],
    )

    assert decision.verification.status == "passed"
    assert decision.verification.checked_items == 2
    assert decision.verification.matched_items == 2
    assert decision.verification.missing_sources == 1
    assert decision.verification.fallback_used is False
    assert decision.warning is None


def test_evidence_audit_agent_falls_back_to_path_evidence():
    agent = EvidenceAuditAgent()

    decision = agent.audit(
        "这是一句无法由本轮图谱证据支持的断言。",
        evidence=[],
        facts=[],
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

    assert decision.verification.status == "fallback"
    assert decision.verification.path_items == 1
    assert decision.verification.missing_sources == 1
    assert decision.verification.fallback_used is True
    assert decision.warning
    assert "HAS_INGREDIENT" in decision.answer
    assert "HAS_EFFECT" in decision.answer


def test_evidence_audit_agent_rejects_partial_attribute_answer():
    agent = EvidenceAuditAgent()

    decision = agent.audit(
        "麻黄的性味是辛、微苦，温。",
        evidence=[],
        facts=[
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
                value="肺经、膀胱经",
            ),
        ],
    )

    assert decision.verification.status == "fallback"
    assert decision.verification.checked_items == 2
    assert decision.verification.matched_items == 1
    assert "归经：肺经、膀胱经" in decision.answer
