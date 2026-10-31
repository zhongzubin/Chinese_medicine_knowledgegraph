# -*- coding: utf-8 -*-
"""LangGraph 问答工作流状态。"""

import operator
from typing import Annotated, List, Optional, TypedDict

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
    IntentResult,
    LinkedEntity,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    PathEvidence,
    QueryResult,
)


class AgentState(TypedDict, total=False):
    request_id: str
    question: str
    normalized_question: str
    rewritten_question: Optional[str]
    session_id: Optional[str]
    conversation_history: List[ConversationTurn]

    intent: Optional[IntentResult]
    prelinked_entities: List[LinkedEntity]
    entity_candidates: List[EntityCandidate]
    linked_entities: List[LinkedEntity]

    attribute_plan: Optional[AttributeQueryPlan]
    attribute_query_attempted: bool
    attribute_relation_fallback: bool
    aggregate_plan: Optional[AggregateQueryPlan]
    multi_condition_plan: Optional[MultiConditionQueryPlan]
    multi_hop_plan: Optional[MultiHopQueryPlan]
    cypher_plan: Optional[CypherPlan]
    query_attempts: int
    validation_errors: Annotated[List[str], operator.add]
    last_error_code: Optional[str]
    is_cypher_valid: bool
    error_code: Optional[str]
    error_message: Optional[str]
    fatal_error: Optional[str]

    query_result: Optional[QueryResult]
    evidence: List[Evidence]
    facts: List[AttributeFact]
    aggregates: List[AggregateFact]
    paths: List[PathEvidence]
    verification: Optional[AnswerVerification]
    answer: str
    grounded: bool
    warnings: Annotated[List[str], operator.add]

    stage_events: Annotated[List[str], operator.add]
