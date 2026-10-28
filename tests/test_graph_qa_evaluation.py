# -*- coding: utf-8 -*-
"""黄金问题集加载和指标汇总测试。"""

from collections import Counter

from graph_qa.evaluate import (
    DEFAULT_CASES_PATH,
    EvaluationCase,
    evaluate_cases,
    evaluate_retrieval,
    load_cases,
    summarize,
    summarize_retrieval,
)
from graph_qa.models import (
    EntityCandidate,
    Evidence,
    GraphQAResponse,
    IntentResult,
    LinkedEntity,
)


class FakeEvaluationEngine:
    def __init__(self, responses):
        self.responses = responses

    def answer(self, question):
        return self.responses[question]


def test_default_qa_cases_are_valid_and_unique():
    cases = load_cases(DEFAULT_CASES_PATH)
    case_ids = [case.id for case in cases]
    path_counts = Counter(
        case.expected_match_path
        for case in cases
        if case.expected_match_path is not None
    )

    assert len(cases) >= 100
    assert len(case_ids) == len(set(case_ids))
    assert {"herb_effect", "formula_ingredient", "non_tcm", "safety"} <= {
        case.category for case in cases
    }
    assert path_counts == {"exact": 50, "fuzzy": 20, "semantic": 20}
    assert all(
        case.retrieval_probe
        for case in cases
        if case.expected_entities
    )
    assert all(
        case.category != "missing_entity" or case.retrieval_probe
        for case in cases
    )


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
        expected_match_path="exact",
        retrieval_probe="麻黄",
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
    assert summary["workflow_paths"]["exact"] == {
        "evaluated": 1,
        "recall_at_3": 1.0,
        "path_match_rate": 1.0,
    }
    assert summary["workflow_path_confusion"]["exact->exact"] == 1
    assert summary["workflow_match_type_counts"]["name"] == 1
    assert summary["pattern_cases_evaluated"] == 1
    assert summary["pattern_match_rate"] == 1.0
    assert summary["grounded_evidence_coverage"] == 1.0
    assert summary["unknown_entity_cases_evaluated"] == 0
    assert summary["unknown_entity_false_recall_rate"] is None
    assert summary["failures"] == []


def test_retrieval_path_metrics_and_unknown_false_recall():
    cases = [
        EvaluationCase(
            id="exact",
            category="exact",
            question="麻黄有什么功效？",
            expected_intent=True,
            expected_entities=[{"label": "Herb", "name": "麻黄"}],
            expected_match_path="exact",
            retrieval_probe="麻黄",
            expected_outcome="grounded_answer",
        ),
        EvaluationCase(
            id="fuzzy",
            category="fuzzy",
            question="小建中烫有哪些组成？",
            expected_intent=True,
            expected_entities=[{"label": "Formula", "name": "小建中汤"}],
            expected_match_path="fuzzy",
            retrieval_probe="小建中烫",
            expected_outcome="grounded_answer",
        ),
        EvaluationCase(
            id="semantic",
            category="semantic_synonym",
            question="大便干硬难排用什么药？",
            expected_intent=True,
            expected_entities=[{"label": "Disease", "name": "肠燥便难"}],
            expected_match_path="semantic",
            retrieval_probe="大便干硬难排",
            expected_outcome="grounded_answer",
        ),
        EvaluationCase(
            id="unknown",
            category="missing_entity",
            question="星辰草有什么功效？",
            expected_intent=True,
            expected_entities=[],
            retrieval_probe="星辰草",
            expected_outcome="clarification",
        ),
    ]

    class FakeEntityIndex:
        def match(
            self,
            candidates,
            question,
            limit_per_label=10,
            fuzzy_threshold=0.72,
        ):
            del question, limit_per_label, fuzzy_threshold
            candidate: EntityCandidate = candidates[0]
            matches = {
                "麻黄": ("Herb", "麻黄", "name"),
                "小建中烫": ("Formula", "小建中汤", "fuzzy"),
                "大便干硬难排": ("Disease", "肠燥便难", "semantic"),
                "星辰草": ("Herb", "七星剑", "semantic"),
            }
            label, name, match_type = matches[candidate.text]
            return [
                LinkedEntity(
                    label=label,
                    name=name,
                    matched_text=candidate.text,
                    match_type=match_type,
                    score=0.8,
                )
            ]

    records = evaluate_retrieval(FakeEntityIndex(), cases)
    summary = summarize_retrieval(records)

    assert summary["total_probes"] == 4
    assert summary["expected_cases_evaluated"] == 3
    assert summary["overall_recall_at_3"] == 1.0
    assert summary["paths"]["exact"]["recall_at_3"] == 1.0
    assert summary["paths"]["fuzzy"]["path_match_rate"] == 1.0
    assert summary["paths"]["semantic"]["path_match_rate"] == 1.0
    assert summary["path_confusion"] == {
        "exact->exact": 1,
        "fuzzy->fuzzy": 1,
        "semantic->semantic": 1,
    }
    assert summary["match_type_counts"] == {
        "name": 1,
        "fuzzy": 1,
        "semantic": 1,
    }
    assert summary["negative_cases_evaluated"] == 1
    assert summary["unknown_entity_false_recall_rate"] == 1.0
    assert summary["failures"][0]["case_id"] == "unknown"
