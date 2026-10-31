# -*- coding: utf-8 -*-
"""黄金问题集加载和指标汇总测试。"""

from collections import Counter
from pathlib import Path

from graph_qa.evaluate import (
    DEFAULT_CASES_PATH,
    EvaluationCase,
    assess_acceptance,
    evaluate_cases,
    evaluate_retrieval,
    load_cases,
    summarize,
    summarize_retrieval,
)
from graph_qa.models import (
    AttributeFact,
    EntityCandidate,
    Evidence,
    ErrorCode,
    GraphQAResponse,
    IntentResult,
    LinkedEntity,
    QueryInfo,
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


def test_product_quality_cases_are_valid():
    cases = load_cases(
        Path("evaluation/product_quality_cases.jsonl")
    )

    assert len(cases) == 16
    assert {case.category for case in cases} == {
        "herb_attribute",
        "formula_attribute",
        "aggregate_count",
    }
    assert all(case.expected_facts or case.expected_aggregates for case in cases)


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
            query=QueryInfo(
                cypher=(
                    "MATCH (subject:Herb)-[:HAS_EFFECT]->(object:Effect) "
                    "WHERE subject.name IN $entity_names RETURN object.name LIMIT 20"
                ),
                parameters={"entity_names": ["麻黄"]},
            ),
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
    assert summary["cypher_cases_evaluated"] == 1
    assert summary["cypher_pass_rate"] == 1.0
    assert summary["query_execution_cases_evaluated"] == 1
    assert summary["query_execution_success_rate"] == 1.0
    assert summary["grounded_evidence_coverage"] == 1.0
    assert summary["unknown_entity_cases_evaluated"] == 0
    assert summary["unknown_entity_false_recall_rate"] is None
    assert summary["failures"] == []


def test_attribute_facts_count_as_grounded_context_and_match_expected_values():
    case = EvaluationCase(
        id="herb-attribute",
        category="herb_attribute",
        question="麻黄性味归经是什么？",
        expected_intent=True,
        expected_entities=[{"label": "Herb", "name": "麻黄"}],
        expected_facts=[
            {
                "subject_label": "Herb",
                "subject_name": "麻黄",
                "attribute": "property_flavor",
                "value_contains": "辛",
            },
            {
                "subject_label": "Herb",
                "subject_name": "麻黄",
                "attribute": "meridian",
                "value_contains": "肺经",
            },
        ],
        expected_match_path="exact",
        retrieval_probe="麻黄",
        expected_outcome="grounded_answer",
    )
    response = GraphQAResponse(
        question=case.question,
        answer="麻黄性味为辛、微苦，温，归肺经、膀胱经。",
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
        facts=[
            AttributeFact(
                subject="麻黄",
                subject_label="Herb",
                attribute="property_flavor",
                attribute_label="性味",
                value="辛、微苦，温",
                source="《中药大辞典》",
            ),
            AttributeFact(
                subject="麻黄",
                subject_label="Herb",
                attribute="meridian",
                attribute_label="归经",
                value="归肺经、膀胱经",
                source="《中药大辞典》",
            ),
        ],
    )

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine({case.question: response}), [case])
    )

    assert summary["outcome_accuracy"] == 1.0
    assert summary["grounded_evidence_coverage"] == 1.0
    assert summary["fact_cases_evaluated"] == 1
    assert summary["fact_match_rate"] == 1.0


def test_effect_attribute_fact_satisfies_relation_pattern_metric():
    case = EvaluationCase(
        id="effect-fact",
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
    response = GraphQAResponse(
        question=case.question,
        answer="麻黄功效为发汗、平喘、利水。",
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
        facts=[
            AttributeFact(
                subject="麻黄",
                subject_label="Herb",
                attribute="effect",
                attribute_label="功效",
                value="发汗，平喘，利水",
            )
        ],
    )

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine({case.question: response}), [case])
    )

    assert summary["pattern_match_rate"] == 1.0


def test_expected_aggregate_matches_count_result():
    case = EvaluationCase(
        id="aggregate-case",
        category="aggregate_count",
        question="含有人参的方剂有多少种？",
        expected_intent=True,
        expected_entities=[{"label": "Herb", "name": "人参"}],
        expected_aggregates=[
            {
                "anchor_label": "Herb",
                "relationship": "HAS_INGREDIENT",
                "result_label": "Formula",
                "count": 128,
            }
        ],
        expected_outcome="grounded_answer",
    )
    response = GraphQAResponse(
        question=case.question,
        answer="根据当前图谱统计，关联到人参的方剂共有 128 个。",
        grounded=True,
        linked_entities=[
            LinkedEntity(
                label="Herb",
                name="人参",
                matched_text="人参",
                match_type="name",
                score=1.0,
            )
        ],
        aggregates=[
            {
                "anchor": "人参",
                "anchor_label": "Herb",
                "relationship": "HAS_INGREDIENT",
                "result_label": "Formula",
                "direction": "incoming",
                "count": 128,
                "description": "关联到人参的方剂",
            }
        ],
    )

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine({case.question: response}), [case])
    )

    assert summary["outcome_accuracy"] == 1.0
    assert summary["aggregate_cases_evaluated"] == 1
    assert summary["aggregate_match_rate"] == 1.0
    assert summary["grounded_evidence_coverage"] == 1.0


def test_query_metrics_keep_execution_failure_after_valid_cypher():
    cases = [
        EvaluationCase(
            id="success",
            category="herb_effect",
            question="麻黄有什么功效？",
            expected_intent=True,
            expected_outcome="grounded_answer",
        ),
        EvaluationCase(
            id="timeout",
            category="herb_effect",
            question="桂枝有什么功效？",
            expected_intent=True,
            expected_outcome="grounded_answer",
        ),
        EvaluationCase(
            id="rejected",
            category="herb_effect",
            question="人参有什么功效？",
            expected_intent=True,
            expected_outcome="grounded_answer",
        ),
    ]
    responses = {
        "麻黄有什么功效？": GraphQAResponse(
            question="麻黄有什么功效？",
            answer="麻黄具有发汗功效。",
            grounded=True,
            query=QueryInfo(
                cypher="MATCH (subject:Herb) RETURN subject.name LIMIT 20",
                parameters={},
            ),
        ),
        "桂枝有什么功效？": GraphQAResponse(
            question="桂枝有什么功效？",
            answer="图谱查询失败。",
            grounded=False,
            error={
                "code": ErrorCode.QUERY_TIMEOUT,
                "message": "图谱查询超时",
            },
        ),
        "人参有什么功效？": GraphQAResponse(
            question="人参有什么功效？",
            answer="查询被拒绝。",
            grounded=False,
            error={
                "code": ErrorCode.CYPHER_REJECTED,
                "message": "查询被拒绝",
            },
        ),
    }

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine(responses), cases)
    )

    assert summary["cypher_cases_evaluated"] == 3
    assert summary["cypher_pass_rate"] == 0.6667
    assert summary["query_execution_cases_evaluated"] == 2
    assert summary["query_execution_success_rate"] == 0.5


def test_acceptance_uses_documented_thresholds_not_outcome_100():
    summary = {
        "outcome_accuracy": 0.95,
        "intent_accuracy": 0.95,
        "entity_recall_at_3": 0.90,
        "cypher_pass_rate": 0.95,
        "query_execution_success_rate": 0.98,
        "grounded_evidence_coverage": 1.0,
        "safety_rejection_rate": 1.0,
        "empty_result_honesty_rate": None,
    }

    acceptance = assess_acceptance(summary)

    assert acceptance["passed"] is True
    assert acceptance["failed_metrics"] == []
    assert acceptance["not_evaluated_metrics"] == [
        "fact_match_rate",
        "aggregate_match_rate",
        "empty_result_honesty_rate"
    ]


def test_acceptance_fails_when_safety_below_required_threshold():
    summary = {
        "intent_accuracy": 1.0,
        "entity_recall_at_3": 1.0,
        "cypher_pass_rate": 1.0,
        "query_execution_success_rate": 1.0,
        "grounded_evidence_coverage": 1.0,
        "safety_rejection_rate": 0.99,
        "empty_result_honesty_rate": 1.0,
    }

    acceptance = assess_acceptance(summary)

    assert acceptance["passed"] is False
    assert acceptance["failed_metrics"] == ["safety_rejection_rate"]


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
