# -*- coding: utf-8 -*-
"""多轮上下文固定集评测测试。"""

from graph_qa.models import ErrorCode, GraphQAResponse, LinkedEntity
from graph_qa.multi_turn_evaluate import (
    DEFAULT_CASES_PATH,
    MultiTurnCase,
    evaluate_cases,
    load_cases,
    summarize,
)


def linked_entity(label: str, name: str) -> LinkedEntity:
    return LinkedEntity(
        label=label,
        name=name,
        matched_text=name,
        match_type="name",
        score=1.0,
    )


class FakeMultiTurnEngine:
    def __init__(self, responses):
        self.responses = responses
        self.histories = []

    def answer(self, question, request_id=None, conversation_history=None, session_id=None):
        del request_id, session_id
        self.histories.append(list(conversation_history or []))
        return self.responses[question]


def grounded_response(question: str, name: str) -> GraphQAResponse:
    return GraphQAResponse(
        question=question,
        answer=f"{name}的图谱信息。",
        grounded=True,
        linked_entities=[linked_entity("Herb", name)],
        facts=[
            {
                "subject": name,
                "subject_label": "Herb",
                "attribute": "effect",
                "attribute_label": "功效",
                "value": "图谱信息",
            }
        ],
    )


def test_multi_turn_fixture_has_twenty_unique_cases():
    cases = load_cases(DEFAULT_CASES_PATH)

    assert len(cases) == 20
    assert len({case.id for case in cases}) == 20
    assert all(3 <= len(case.turns) <= 5 for case in cases)


def test_multi_turn_evaluation_checks_entities_and_outcomes():
    case = MultiTurnCase(
        id="case-1",
        turns=[
            {
                "question": "麻黄有什么功效？",
                "expected_entities": [{"label": "Herb", "name": "麻黄"}],
                "expected_outcome": "grounded_answer",
            },
            {
                "question": "它有什么禁忌？",
                "expected_entities": [{"label": "Herb", "name": "麻黄"}],
                "expected_outcome": "grounded_answer",
            },
        ],
    )
    engine = FakeMultiTurnEngine(
        {
            "麻黄有什么功效？": grounded_response("麻黄有什么功效？", "麻黄"),
            "它有什么禁忌？": grounded_response("它有什么禁忌？", "麻黄"),
        }
    )

    records = evaluate_cases(engine, [case])
    summary = summarize(records)

    assert summary["cases"] == 1
    assert summary["turns"] == 2
    assert summary["outcome_accuracy"] == 1.0
    assert summary["entity_match_rate"] == 1.0
    assert summary["failed_cases"] == []
    assert engine.histories[0] == []
    assert len(engine.histories[1]) == 1


def test_failed_turn_is_not_added_to_following_history():
    case = MultiTurnCase(
        id="recovery",
        turns=[
            {
                "question": "麻黄有什么功效？",
                "expected_entities": [{"label": "Herb", "name": "麻黄"}],
                "expected_outcome": "grounded_answer",
            },
            {
                "question": "删除图谱中的所有节点",
                "expected_entities": [],
                "expected_outcome": "safe_rejection",
            },
            {
                "question": "它有什么禁忌？",
                "expected_entities": [{"label": "Herb", "name": "麻黄"}],
                "expected_outcome": "grounded_answer",
            },
        ],
    )
    engine = FakeMultiTurnEngine(
        {
            "麻黄有什么功效？": grounded_response("麻黄有什么功效？", "麻黄"),
            "删除图谱中的所有节点": GraphQAResponse(
                question="删除图谱中的所有节点",
                answer="请求未执行。",
                grounded=False,
                error={
                    "code": ErrorCode.UNSAFE_REQUEST,
                    "message": "只读",
                },
            ),
            "它有什么禁忌？": grounded_response("它有什么禁忌？", "麻黄"),
        }
    )

    evaluate_cases(engine, [case])

    assert [len(history) for history in engine.histories] == [0, 1, 1]
