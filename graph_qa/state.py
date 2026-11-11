# -*- coding: utf-8 -*-
"""LangGraph 问答工作流状态。"""

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from graph_qa.models import (
    AggregateFact,
    AggregateQueryPlan,
    AnswerVerification,
    AttributeFact,
    AttributeQueryPlan,
    CypherPlan,
    ConversationTurn,
    EntityCandidate,
    Evidence,
    GraphAnswerDraft,
    IntentResult,
    LinkedEntity,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    PathEvidence,
    QueryResult,
    RetrievalPlanKind,
    StageTiming,
)


class AgentState(TypedDict, total=False):
    request_id: str     # 请求ID
    question: str     # 用户问题
    normalized_question: str     # 归一化后的用户问题
    rewritten_question: Optional[str]     # 重写后的用户问题
    session_id: Optional[str]     # 会话ID
    conversation_history: List[ConversationTurn]     # 会话历史

    intent: Optional[IntentResult]  # 意图识别
    prelinked_entities: List[LinkedEntity]     # 预链接实体
    entity_candidates: List[EntityCandidate]     # 实体候选项
    linked_entities: List[LinkedEntity]     # 链接实体

    attribute_plan: Optional[AttributeQueryPlan]     # 属性查询计划
    attribute_query_attempted: bool     # 属性查询是否尝试
    attribute_relation_fallback: bool     # 属性关系回退
    aggregate_plan: Optional[AggregateQueryPlan]     # 聚合查询计划
    multi_condition_plan: Optional[MultiConditionQueryPlan]     # 多条件查询计划
    multi_hop_plan: Optional[MultiHopQueryPlan]     # 多跳查询计划
    cypher_plan: Optional[CypherPlan]     # Cypher查询计划
    retrieval_plan_kind: Optional[RetrievalPlanKind]     # 检索计划类型
    retrieval_steps: int     # 检索步骤数
    retrieval_next_action: Optional[str]     # 检索下一步操作
    retrieval_trace: Annotated[List[Dict[str, Any]], operator.add]     # 检索跟踪
    query_attempts: int     # 查询尝试次数
    validation_errors: Annotated[List[str], operator.add]     # 验证错误
    last_error_code: Optional[str]     # 最后错误码
    is_cypher_valid: bool     # Cypher是否有效
    error_code: Optional[str]     # 错误码
    error_message: Optional[str]     # 错误信息
    fatal_error: Optional[str]     # 严重错误

    query_result: Optional[QueryResult]     # 查询结果
    evidence: List[Evidence]     # 证据
    facts: List[AttributeFact]     # 事实
    aggregates: List[AggregateFact]     # 聚合事实
    paths: List[PathEvidence]     # 路径证据
    answer_draft: Optional[GraphAnswerDraft]     # 结构化回答草稿
    verification: Optional[AnswerVerification]     # 答案验证
    answer: str     # 答案
    grounded: bool     # 是否 grounded
    warnings: Annotated[List[str], operator.add]     # 警告
    stage_timings: Annotated[List[StageTiming], operator.add]     # 阶段耗时

    stage_events: Annotated[List[str], operator.add]
