# -*- coding: utf-8 -*-
"""黄金问题集加载和指标汇总测试。"""

from graph_qa.evaluate import (
    DEFAULT_CASES_PATH,
    EvaluationCase,
    evaluate_cases,
    load_cases,
    summarize,
)
from graph_qa.models import Evidence, GraphQAResponse, IntentResult, LinkedEntity


class FakeEvaluationEngine:
    def __init__(self, responses):
        self.responses = responses

    def answer(self, question):
        return self.responses[question]


def test_default_qa_cases_are_valid_and_unique():
    cases = load_cases(DEFAULT_CASES_PATH)
    case_ids = [case.id for case in cases]

    assert len(cases) >= 30
    assert len(case_ids) == len(set(case_ids))
    assert {"herb_effect", "formula_ingredient", "non_tcm", "safety"} <= {
        case.category for case in cases
    }


def test_evaluate_cases_and_summary_metrics():
    grounded_case = EvaluationCase(
        id="grounded-case",
        category="herb_effect",
        question="麻黄有什么功效？",
        expected_intent=True,
        expected_entities=[{"label": "Herb", "name": "麻黄"}],
        expected_patterns=[
            {
                "subject_label": "Herb",
                "relationship": "HAS_EFFECT",
                "object_label": "Effect",
            }
        ],
        expected_outcome="grounded_answer",
    )
    direct_case = EvaluationCase(
        id="direct-case",
        category="non_tcm",
        question="1 加 1 等于多少？",
        expected_intent=False,
        expected_entities=[],
        expected_patterns=[],
        expected_outcome="direct_answer",
    )
    responses = {
        grounded_case.question: GraphQAResponse(
            question=grounded_case.question,
            answer="麻黄可以发汗。\n\n以上内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。",
            grounded=True,
            intent=IntentResult(
                is_tcm_question=True,
                confidence=1.0,
                reason_code="TCM_ENTITY_PRESENT",
            ),
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
                )
            ],
        ),
        direct_case.question: GraphQAResponse(
            question=direct_case.question,
            answer="等于 2。",
            grounded=False,
            intent=IntentResult(
                is_tcm_question=False,
                confidence=1.0,
                reason_code="OUT_OF_SCOPE",
            ),
            warnings=["该回答未使用知识图谱证据。"],
        ),
    }

    records = evaluate_cases(
        FakeEvaluationEngine(responses),
        [grounded_case, direct_case],
    )
    summary = summarize(records)

    assert summary["total"] == 2
    assert summary["outcome_accuracy"] == 1.0
    assert summary["intent_evaluated"] == 2
    assert summary["intent_accuracy"] == 1.0
    assert summary["entity_cases_evaluated"] == 1
    assert summary["entity_recall_at_3"] == 1.0
    assert summary["pattern_cases_evaluated"] == 1
    assert summary["pattern_match_rate"] == 1.0
    assert summary["grounded_evidence_coverage"] == 1.0
    assert summary["failures"] == []
