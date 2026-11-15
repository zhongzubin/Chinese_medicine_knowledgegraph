# -*- coding: utf-8 -*-
"""受控检索 Agent 的独立 LangGraph 子图。"""

from typing import Any, Callable, Dict, Mapping, Optional

from langgraph.graph import END, START, StateGraph

from graph_qa.state import AgentState

RETRIEVAL_STAGE_NAMES = (
    "plan_cypher",
    "validate_cypher",
    "execute_cypher",
    "retrieve_observe",
)
RETRIEVAL_ADDITIVE_FIELDS = (
    "retrieval_trace",
    "validation_errors",
    "warnings",
    "stage_timings",
    "stage_events",
)
RETRIEVAL_PARENT_FIELDS = (
    "attribute_plan",
    "attribute_query_attempted",
    "attribute_relation_fallback",
    "aggregate_plan",
    "multi_condition_plan",
    "multi_hop_plan",
    "cypher_plan",
    "retrieval_plan_kind",
    "retrieval_steps",
    "retrieval_next_action",
    "query_attempts",
    "last_error_code",
    "is_cypher_valid",
    "error_code",
    "error_message",
    "fatal_error",
    "query_result",
    "evidence",
    "facts",
    "aggregates",
    "paths",
    "answer",
    "grounded",
    *RETRIEVAL_ADDITIVE_FIELDS,
)

NodeHandler = Callable[[AgentState], Dict[str, Any]]
NodeWrapper = Callable[[str, NodeHandler], NodeHandler]


def route_after_validate(state: AgentState) -> str:
    """校验成功进入执行，否则返回观察节点决定是否重规划。"""
    return "execute_cypher" if state.get("is_cypher_valid") else "retrieve_observe"


def route_after_observe(state: AgentState) -> str:
    """把观察动作映射为子图内部路由。"""
    action = state.get("retrieval_next_action") or "finish"
    return "plan_cypher" if action == "replan" else action


def build_retrieval_subgraph(
    *,
    plan_cypher: NodeHandler,
    validate_cypher: NodeHandler,
    execute_cypher: NodeHandler,
    retrieve_observe: NodeHandler,
    wrap_node: Optional[NodeWrapper] = None,
):
    """构建只负责检索规划、校验、执行和观察的子图。"""
    handlers: Mapping[str, NodeHandler] = {
        "plan_cypher": plan_cypher,
        "validate_cypher": validate_cypher,
        "execute_cypher": execute_cypher,
        "retrieve_observe": retrieve_observe,
    }
    builder = StateGraph(AgentState)
    for stage in RETRIEVAL_STAGE_NAMES:
        handler = handlers[stage]
        builder.add_node(stage, wrap_node(stage, handler) if wrap_node else handler)

    builder.add_edge(START, "plan_cypher")
    builder.add_edge("plan_cypher", "validate_cypher")
    builder.add_conditional_edges(
        "validate_cypher",
        route_after_validate,
        {
            "execute_cypher": "execute_cypher",
            "retrieve_observe": "retrieve_observe",
        },
    )
    builder.add_edge("execute_cypher", "retrieve_observe")
    builder.add_conditional_edges(
        "retrieve_observe",
        route_after_observe,
        {
            "plan_cypher": "plan_cypher",
            "answer_from_graph": END,
            "no_evidence": END,
            "safe_fail": END,
            "finish": END,
        },
    )
    return builder.compile()


def retrieval_parent_update(
    initial_state: Mapping[str, Any],
    final_state: Mapping[str, Any],
) -> Dict[str, Any]:
    """把子图最终状态转换为父图可安全合并的增量更新。"""
    update: Dict[str, Any] = {}
    for field in RETRIEVAL_PARENT_FIELDS:
        if field not in final_state:
            continue
        value = final_state[field]
        if field in RETRIEVAL_ADDITIVE_FIELDS:
            previous = initial_state.get(field) or []
            value = list(value or [])[len(previous):]
            if not value:
                continue
        update[field] = value
    return update
