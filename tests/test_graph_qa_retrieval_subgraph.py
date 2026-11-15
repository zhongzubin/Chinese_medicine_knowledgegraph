# -*- coding: utf-8 -*-
"""独立检索 Agent 子图测试。"""

from graph_qa.agents.retrieval_subgraph import (
    build_retrieval_subgraph,
    retrieval_parent_update,
)


def test_retrieval_subgraph_answers_after_successful_execution():
    calls = []

    def plan(state):
        calls.append("plan")
        return {
            "retrieval_steps": state.get("retrieval_steps", 0) + 1,
            "retrieval_next_action": None,
        }

    def validate(_state):
        calls.append("validate")
        return {"is_cypher_valid": True}

    def execute(_state):
        calls.append("execute")
        return {"evidence": []}

    def observe(_state):
        calls.append("observe")
        return {
            "retrieval_next_action": "answer_from_graph",
            "retrieval_trace": [
                {
                    "step": 1,
                    "next_action": "answer_from_graph",
                    "reason_code": "VERIFIABLE_CONTEXT_AVAILABLE",
                }
            ],
        }

    subgraph = build_retrieval_subgraph(
        plan_cypher=plan,
        validate_cypher=validate,
        execute_cypher=execute,
        retrieve_observe=observe,
    )
    result = subgraph.invoke({"retrieval_steps": 0, "retrieval_trace": []})

    assert calls == ["plan", "validate", "execute", "observe"]
    assert result["retrieval_next_action"] == "answer_from_graph"
    assert result["retrieval_steps"] == 1


def test_retrieval_subgraph_replans_then_safely_stops():
    plan_calls = 0

    def plan(state):
        nonlocal plan_calls
        plan_calls += 1
        return {"retrieval_steps": state.get("retrieval_steps", 0) + 1}

    def validate(_state):
        return {"is_cypher_valid": False}

    def execute(_state):
        raise AssertionError("校验失败时不应执行查询")

    def observe(state):
        action = "replan" if state.get("retrieval_steps", 0) < 3 else "safe_fail"
        return {"retrieval_next_action": action}

    subgraph = build_retrieval_subgraph(
        plan_cypher=plan,
        validate_cypher=validate,
        execute_cypher=execute,
        retrieve_observe=observe,
    )
    result = subgraph.invoke({"retrieval_steps": 0, "retrieval_trace": []})

    assert plan_calls == 3
    assert result["retrieval_steps"] == 3
    assert result["retrieval_next_action"] == "safe_fail"


def test_parent_update_only_appends_new_additive_items():
    old_timing = {"stage": "old", "elapsed_ms": 1}
    new_timing = {"stage": "plan_cypher", "elapsed_ms": 2}
    initial = {
        "stage_timings": [old_timing],
        "retrieval_trace": [],
        "retrieval_steps": 0,
        "evidence": [],
    }
    evidence = [{"subject": "麻黄"}]
    final = {
        "stage_timings": [old_timing, new_timing],
        "retrieval_trace": [{"step": 1}],
        "retrieval_steps": 1,
        "evidence": evidence,
    }

    update = retrieval_parent_update(initial, final)

    assert update["stage_timings"] == [new_timing]
    assert update["retrieval_trace"] == [{"step": 1}]
    assert update["retrieval_steps"] == 1
    assert update["evidence"] is evidence
