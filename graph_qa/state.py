# -*- coding: utf-8 -*-
"""LangGraph 问答工作流状态。"""

import operator
from typing import Annotated, List, Optional, TypedDict

from graph_qa.models import (
    CypherPlan,
    EntityCandidate,
    Evidence,
    IntentResult,
    LinkedEntity,
    QueryResult,
)


class AgentState(TypedDict, total=False):
    request_id: str
    question: str
    normalized_question: str

    intent: Optional[IntentResult]
    prelinked_entities: List[LinkedEntity]
    entity_candidates: List[EntityCandidate]
    linked_entities: List[LinkedEntity]

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
    answer: str
    grounded: bool
    warnings: Annotated[List[str], operator.add]

    stage_events: Annotated[List[str], operator.add]
