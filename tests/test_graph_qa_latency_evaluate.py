# -*- coding: utf-8 -*-
"""工作流阶段耗时评测测试。"""

from graph_qa.evaluate import EvaluationCase
from graph_qa.latency_evaluate import evaluate_latency, summarize_latency
from graph_qa.models import GraphQAResponse, StageTiming


def make_case(case_id: str, question: str) -> EvaluationCase:
    return EvaluationCase(
        id=case_id,
        category="attribute",
        question=question,
        expected_intent=True,
        expected_outcome="grounded_answer",
    )


def test_summarize_latency_calculates_stage_distribution_and_share():
    records = [
        {
            "total_ms": 100.0,
            "stage_timings": [
                StageTiming(stage="plan_cypher", elapsed_ms=40.0),
                StageTiming(stage="answer_from_graph", elapsed_ms=30.0),
            ],
        },
        {
            "total_ms": 200.0,
            "stage_timings": [
                StageTiming(stage="plan_cypher", elapsed_ms=80.0),
                StageTiming(stage="answer_from_graph", elapsed_ms=60.0),
            ],
        },
    ]

    summary = summarize_latency(records)

    assert summary["samples"] == 2
    assert summary["request_total_ms"] == {
        "mean": 150.0,
        "p50": 100.0,
        "p95": 200.0,
        "max": 200.0,
        "total": 300.0,
    }
    assert summary["stages"][0] == {
        "stage": "plan_cypher",
        "calls": 2,
        "mean": 60.0,
        "p50": 40.0,
        "p95": 80.0,
        "max": 80.0,
        "total_ms": 120.0,
        "share": 0.4,
    }
    assert summary["stages"][1]["stage"] == "answer_from_graph"
    assert summary["stages"][1]["share"] == 0.3


def test_evaluate_latency_collects_response_and_stage_timings():
    class FakeTimedEngine:
        def __init__(self):
            self.requests = []

        def answer_with_timings(self, question, request_id=None):
            self.requests.append((question, request_id))
            return (
                GraphQAResponse(
                    question=question,
                    answer="根据图谱回答。",
                    grounded=True,
                ),
                [
                    StageTiming(stage="normalize", elapsed_ms=1.25),
                    StageTiming(stage="verify_answer", elapsed_ms=2.5),
                ],
            )

    engine = FakeTimedEngine()
    records = evaluate_latency(
        engine,
        [make_case("sample-1", "麻黄有什么功效？")],
    )

    assert engine.requests == [
        ("麻黄有什么功效？", "latency-sample-1")
    ]
    assert records[0]["case_id"] == "sample-1"
    assert records[0]["grounded"] is True
    assert records[0]["error_code"] is None
    assert records[0]["stage_timings"] == [
        {"stage": "normalize", "elapsed_ms": 1.25},
        {"stage": "verify_answer", "elapsed_ms": 2.5},
    ]


def test_summarize_latency_handles_empty_records():
    summary = summarize_latency([])

    assert summary == {
        "samples": 0,
        "request_total_ms": {
            "mean": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
            "total": 0.0,
        },
        "stages": [],
    }
