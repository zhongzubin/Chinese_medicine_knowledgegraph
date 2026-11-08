# -*- coding: utf-8 -*-
"""独立人工事实审核流程测试。"""

import pytest

from graph_qa.human_fact_audit import (
    build_review_records,
    sample_evaluation_records,
    summarize_reviews,
)


def make_record(index: int, category: str) -> dict:
    return {
        "case_id": f"case-{index}",
        "category": category,
        "question": f"问题 {index}",
        "expected_outcome": "grounded_answer",
        "response": {
            "answer": f"回答 {index}",
            "linked_entities": [],
            "evidence": [],
            "facts": [],
            "aggregates": [],
            "paths": [],
            "verification": None,
        },
    }


def test_sampling_is_deterministic_and_stratified():
    records = [
        make_record(index, ("attribute", "relationship", "multi_turn")[index % 3])
        for index in range(30)
    ]

    first = sample_evaluation_records(records, count=9, seed=42)
    second = sample_evaluation_records(records, count=9, seed=42)

    assert [item["case_id"] for item in first] == [
        item["case_id"] for item in second
    ]
    assert {
        item["category"] for item in first
    } == {"attribute", "relationship", "multi_turn"}
    assert len(first) == 9


def test_build_review_records_hides_automatic_expectations():
    evaluation = {
        "records": [
            make_record(index, "attribute")
            for index in range(3)
        ]
    }

    reviews = build_review_records(evaluation, count=2, seed=7)

    assert len(reviews) == 2
    assert all(item["review_status"] == "pending" for item in reviews)
    assert all("expected_outcome" not in item for item in reviews)
    assert reviews[0]["graph_answer"].startswith("回答")


def test_pending_reviews_are_not_treated_as_passed():
    summary = summarize_reviews(
        [
            {
                "review_status": "pending",
                "fact_verdict": None,
                "unsupported_claims": [],
                "source_disclosure_correct": None,
            }
        ]
    )

    assert summary["completed"] == 0
    assert summary["fact_accuracy_rate"] is None
    assert summary["acceptance"]["passed"] is False
    assert "reviewed_cases" in summary["not_evaluated_metrics"]


def test_completed_reviews_pass_quality_gate():
    reviews = [
        {
            "review_status": "completed",
            "fact_verdict": "correct",
            "unsupported_claims": [],
            "source_disclosure_correct": True,
        }
        for _ in range(50)
    ]

    summary = summarize_reviews(reviews)

    assert summary["fact_accuracy_rate"] == 1.0
    assert summary["source_disclosure_rate"] == 1.0
    assert summary["acceptance"] == {
        "passed": True,
        "failed_metrics": [],
    }


def test_review_gate_fails_for_partial_answers_or_unsupported_claims():
    reviews = [
        {
            "review_status": "completed",
                "fact_verdict": "partial" if index < 3 else "correct",
            "unsupported_claims": ["虚构剂量"] if index == 1 else [],
            "source_disclosure_correct": True,
        }
        for index in range(50)
    ]

    summary = summarize_reviews(reviews)

    assert summary["fact_accuracy_rate"] < 0.95
    assert summary["unsupported_claim_count"] == 1
    assert set(summary["acceptance"]["failed_metrics"]) == {
        "fact_accuracy_rate",
        "unsupported_claim_count",
    }


def test_completed_review_requires_known_fact_verdict():
    with pytest.raises(ValueError, match="未知事实审核结论"):
        summarize_reviews(
            [
                {
                    "review_status": "completed",
                    "fact_verdict": "unknown",
                    "unsupported_claims": [],
                    "source_disclosure_correct": True,
                }
            ]
        )
