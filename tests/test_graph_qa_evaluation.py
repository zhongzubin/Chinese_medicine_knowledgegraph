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
    PathEvidence,
    PathStepEvidence,
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

    assert len(cases) == 220
    assert {case.category for case in cases} == {
        "herb_attribute",
        "formula_attribute",
        "relation_forward",
        "relation_reverse",
        "aggregate_count",
        "multi_condition_list",
        "multi_hop",
        "dynamic_multi_hop",
        "dynamic_multi_hop_negative",
        "semantic_synonym",
        "unknown_entity",
        "no_evidence",
        "source_missing",
    }
    assert len({case.id for case in cases}) == len(cases)
    assert len({case.question for case in cases}) == len(cases)
    assert all(
        case.expected_facts
        or case.expected_aggregates
        or case.expected_answer_contains
        or case.expected_patterns
        or case.expected_paths
        or case.expected_outcome
        in {"clarification", "no_evidence", "safe_rejection"}
        for case in cases
    )
    assert sum(case.category == "unknown_entity" for case in cases) == 20
    assert sum(case.category == "no_evidence" for case in cases) == 15
    assert sum(case.category == "source_missing" for case in cases) == 10


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


def test_no_evidence_clarification_and_missing_source_metrics():
    no_evidence_case = EvaluationCase(
        id="no-evidence",
        category="no_evidence",
        question="人参怎么用？",
        expected_intent=True,
        expected_entities=[{"label": "Herb", "name": "人参"}],
        expected_outcome="no_evidence",
    )
    clarification_case = EvaluationCase(
        id="unknown",
        category="unknown_entity",
        question="玄霜参有什么功效？",
        expected_intent=True,
        expected_entities=[],
        retrieval_probe="玄霜参",
        expected_outcome="clarification",
    )
    source_missing_case = EvaluationCase(
        id="source-missing",
        category="source_missing",
        question="麻黄性味是什么？",
        expected_intent=True,
        expected_entities=[{"label": "Herb", "name": "麻黄"}],
        expected_facts=[
            {
                "subject_label": "Herb",
                "subject_name": "麻黄",
                "attribute": "property_flavor",
                "value_contains": "辛",
            }
        ],
        expected_source_status="missing",
        expected_outcome="grounded_answer",
    )
    responses = {
        no_evidence_case.question: GraphQAResponse(
            question=no_evidence_case.question,
            answer=(
                "已识别到相关图谱实体，但当前图谱没有找到"
                "足以回答该问题的依据。"
            ),
            grounded=False,
        ),
        clarification_case.question: GraphQAResponse(
            question=clarification_case.question,
            answer=(
                "当前图谱未识别出具体实体。"
                "请补充或确认具体名称后再试。"
            ),
            grounded=False,
        ),
        source_missing_case.question: GraphQAResponse(
            question=source_missing_case.question,
            answer="麻黄的性味：辛、微苦，温（未提供出处）",
            grounded=True,
            facts=[
                AttributeFact(
                    subject="麻黄",
                    subject_label="Herb",
                    attribute="property_flavor",
                    attribute_label="性味",
                    value="辛、微苦，温",
                )
            ],
        ),
    }

    summary = summarize(
        evaluate_cases(
            FakeEvaluationEngine(responses),
            [
                no_evidence_case,
                clarification_case,
                source_missing_case,
            ],
        )
    )

    assert summary["no_evidence_cases_evaluated"] == 1
    assert summary["no_evidence_honesty_rate"] == 1.0
    assert summary["clarification_cases_evaluated"] == 1
    assert summary["clarification_accuracy"] == 1.0
    assert summary["source_missing_cases_evaluated"] == 1
    assert summary["source_missing_disclosure_rate"] == 1.0


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


def test_multi_condition_list_metrics_check_answer_and_evidence_objects():
    case = EvaluationCase(
        id="multi-condition-list",
        category="multi_condition_list",
        question="同时含有人参和白术的方剂有哪些？",
        expected_intent=True,
        expected_entities=[
            {"label": "Herb", "name": "人参"},
            {"label": "Herb", "name": "白术"},
        ],
        expected_patterns=[
            {
                "subject_label": "Formula",
                "relationship": "HAS_INGREDIENT",
                "object_label": "Herb",
            }
        ],
        expected_answer_contains=["三痹汤"],
        expected_evidence_objects=["人参", "白术"],
        expected_outcome="grounded_answer",
    )
    response = GraphQAResponse(
        question=case.question,
        answer="根据当前图谱，同时关联到人参、白术的方剂如下：\n- 三痹汤",
        grounded=True,
        linked_entities=[
            LinkedEntity(
                label="Herb",
                name="人参",
                matched_text="人参",
                match_type="name",
                score=1.0,
            ),
            LinkedEntity(
                label="Herb",
                name="白术",
                matched_text="白术",
                match_type="name",
                score=1.0,
            ),
        ],
        evidence=[
            Evidence(
                subject="三痹汤",
                subject_label="Formula",
                relationship="HAS_INGREDIENT",
                object="人参",
                object_label="Herb",
            ),
            Evidence(
                subject="三痹汤",
                subject_label="Formula",
                relationship="HAS_INGREDIENT",
                object="白术",
                object_label="Herb",
            ),
        ],
    )

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine({case.question: response}), [case])
    )

    assert summary["answer_contains_match_rate"] == 1.0
    assert summary["evidence_object_match_rate"] == 1.0
    assert summary["pattern_match_rate"] == 1.0


def test_expected_multi_hop_path_matches_response():
    case = EvaluationCase(
        id="multi-hop-path",
        category="multi_hop",
        question="四君子汤所含药材有什么功效？",
        expected_intent=True,
        expected_entities=[{"label": "Formula", "name": "四君子汤"}],
        expected_paths=[
            {
                "root_label": "Formula",
                "root_name": "四君子汤",
                "first_relationship": "HAS_INGREDIENT",
                "intermediate_label": "Herb",
                "second_relationship": "HAS_EFFECT",
                "target_label": "Effect",
            }
        ],
        expected_outcome="grounded_answer",
    )
    response = GraphQAResponse(
        question=case.question,
        answer="四君子汤中的人参可养血。",
        grounded=True,
        paths=[
            {
                "root": "四君子汤",
                "root_label": "Formula",
                "first_relationship": "HAS_INGREDIENT",
                "intermediate": "人参",
                "intermediate_label": "Herb",
                "second_relationship": "HAS_EFFECT",
                "target": "养血",
                "target_label": "Effect",
            }
        ],
    )

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine({case.question: response}), [case])
    )

    assert summary["path_cases_evaluated"] == 1
    assert summary["path_match_rate_product"] == 1.0
    assert summary["grounded_evidence_coverage"] == 1.0


def test_expected_three_step_path_matches_response():
    case = EvaluationCase(
        id="three-step-path",
        category="multi_hop",
        question="四君子汤所含药材治疗的疾病有哪些症状？",
        expected_intent=True,
        expected_entities=[{"label": "Formula", "name": "四君子汤"}],
        expected_paths=[
            {
                "root_label": "Formula",
                "root_name": "四君子汤",
                "steps": [
                    {
                        "subject_label": "Formula",
                        "relationship": "HAS_INGREDIENT",
                        "object_label": "Herb",
                    },
                    {
                        "subject_label": "Herb",
                        "relationship": "TREATS_DISEASE",
                        "object_label": "Disease",
                    },
                    {
                        "subject_label": "Disease",
                        "relationship": "HAS_SYMPTOM",
                        "object_label": "Symptom",
                    },
                ],
            }
        ],
        expected_outcome="grounded_answer",
    )
    response = GraphQAResponse(
        question=case.question,
        answer="四君子汤所含药材治疗的疾病可出现乏力。",
        grounded=True,
        paths=[
            PathEvidence(
                root="四君子汤",
                root_label="Formula",
                target="乏力",
                target_label="Symptom",
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
                        relationship="TREATS_DISEASE",
                        object="气虚证",
                        object_label="Disease",
                    ),
                    PathStepEvidence(
                        subject="气虚证",
                        subject_label="Disease",
                        relationship="HAS_SYMPTOM",
                        object="乏力",
                        object_label="Symptom",
                    ),
                ],
            )
        ],
    )

    summary = summarize(
        evaluate_cases(FakeEvaluationEngine({case.question: response}), [case])
    )

    assert summary["path_cases_evaluated"] == 1
    assert summary["path_match_rate_product"] == 1.0


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
        "answer_contains_match_rate",
        "evidence_object_match_rate",
        "path_match_rate_product",
        "empty_result_honesty_rate",
        "no_evidence_honesty_rate",
        "clarification_accuracy",
        "source_missing_disclosure_rate",
        "unknown_entity_false_recall_rate",
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


def test_acceptance_fails_when_unknown_entity_false_recall_is_above_zero():
    summary = {
        "intent_accuracy": 1.0,
        "entity_recall_at_3": 1.0,
        "cypher_pass_rate": 1.0,
        "query_execution_success_rate": 1.0,
        "grounded_evidence_coverage": 1.0,
        "safety_rejection_rate": 1.0,
        "empty_result_honesty_rate": 1.0,
        "no_evidence_honesty_rate": 1.0,
        "clarification_accuracy": 1.0,
        "source_missing_disclosure_rate": 1.0,
        "unknown_entity_false_recall_rate": 0.1,
    }

    acceptance = assess_acceptance(summary)

    assert acceptance["passed"] is False
    assert acceptance["failed_metrics"] == [
        "unknown_entity_false_recall_rate"
    ]
    assert (
        acceptance["checks"]["unknown_entity_false_recall_rate"][
            "comparison"
        ]
        == "<="
    )


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
