# -*- coding: utf-8 -*-
"""问答角色 Agent 的注册与工作流映射测试。"""

import pytest

from graph_qa.agents import (
    AgentDescriptor,
    AgentRegistry,
    EvidenceAuditAgent,
    QAAgentTeam,
)
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.models import AttributeFact, Evidence, PathEvidence
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
    "answer_from_graph",
    "verify_answer",
    "finish",
)


def make_schema() -> GraphSchema:
    return GraphSchema.from_mapping(
        {
            "format_version": "1.1",
            "node_labels": [
                {"label": label, "properties": [{"name": "name"}]}
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


def test_agent_registry_has_unique_names_and_stage_owners():
    team = QAAgentTeam(reasoner=object())
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
    team = QAAgentTeam(reasoner=object())

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
    team = QAAgentTeam(reasoner=object())

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
