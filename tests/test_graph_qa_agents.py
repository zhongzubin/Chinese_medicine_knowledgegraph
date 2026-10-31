# -*- coding: utf-8 -*-
"""问答角色 Agent 的注册与工作流映射测试。"""

import pytest

from graph_qa.agents import AgentDescriptor, AgentRegistry, QAAgentTeam
from graph_qa.entity_linker import EntityIndex, EntityRecord
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
