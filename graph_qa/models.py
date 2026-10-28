# -*- coding: utf-8 -*-
"""知识图谱问答的数据模型和稳定性约定。"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

EntityLabel = Literal["Herb", "Formula", "Disease", "Symptom", "Effect", "Source"]
MatchType = Literal["name", "alias", "fuzzy", "semantic"]

MEDICAL_DISCLAIMER = "以上内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。"


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSAFE_REQUEST = "UNSAFE_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_BUSY = "SERVICE_BUSY"
    RATE_LIMIT_UNAVAILABLE = "RATE_LIMIT_UNAVAILABLE"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_BUSY = "SESSION_BUSY"
    SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"
    CONFIG_ERROR = "CONFIG_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    CYPHER_REJECTED = "CYPHER_REJECTED"
    CYPHER_INVALID = "CYPHER_INVALID"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    QUERY_EXECUTION_FAILED = "QUERY_EXECUTION_FAILED"
    NO_GRAPH_EVIDENCE = "NO_GRAPH_EVIDENCE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GraphQARequest(StrictModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class APIQARequest(GraphQARequest):
    session_id: Optional[UUID] = None


class IntentResult(StrictModel):
    is_tcm_question: bool
    confidence: float = Field(ge=0.0, le=1.0)
    matched_domain_terms: List[str] = Field(default_factory=list)
    reason_code: str = Field(min_length=1, max_length=80)


class EntityCandidate(StrictModel):
    text: str = Field(min_length=1, max_length=100)
    suggested_label: Optional[EntityLabel] = None

    @field_validator("suggested_label", mode="before")
    @classmethod
    def normalize_unknown_label(cls, value: Any) -> Optional[str]:
        allowed = {"Herb", "Formula", "Disease", "Symptom", "Effect", "Source"}
        return value if isinstance(value, str) and value in allowed else None

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("实体候选不能为空")
        return value


class LinkedEntity(StrictModel):
    label: EntityLabel
    name: str = Field(min_length=1, max_length=100)
    matched_text: str = Field(min_length=1, max_length=100)
    match_type: MatchType
    score: float = Field(ge=0.0, le=1.0)


class UsedEntityRef(StrictModel):
    label: EntityLabel
    name: str = Field(min_length=1, max_length=100)


class ConversationTurn(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=10000)
    linked_entities: List[LinkedEntity] = Field(default_factory=list)
    grounded: bool
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class RewriteResult(StrictModel):
    standalone_question: str = Field(min_length=1, max_length=500)

    @field_validator("standalone_question")
    @classmethod
    def strip_standalone_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("改写后的问题不能为空")
        return value


def _validate_parameter_value(value: Any) -> None:
    if isinstance(value, (str, bool, int, float)):
        return
    if isinstance(value, list):
        first_type = None
        for item in value:
            if not isinstance(item, (str, bool, int, float)):
                raise ValueError("查询参数数组只能包含字符串、布尔值、整数或浮点数")
            item_type = type(item)
            if first_type is None:
                first_type = item_type
            elif item_type is not first_type:
                raise ValueError("Neo4j 查询参数数组必须是同一种基本类型")
        return
    raise ValueError("查询参数只允许基本类型或同类型的基本类型数组")


class CypherPlan(StrictModel):
    cypher: str = Field(min_length=1)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    used_entities: List[UsedEntityRef] = Field(default_factory=list)
    expected_result_shape: str = Field(min_length=1, max_length=120)

    @field_validator("parameters")
    @classmethod
    def validate_parameters(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        for parameter_value in value.values():
            _validate_parameter_value(parameter_value)
        return value


class QueryInfo(StrictModel):
    cypher: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


class QueryResult(StrictModel):
    cypher: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    limit_applied: int = Field(ge=1)


class Evidence(StrictModel):
    subject: str
    subject_label: EntityLabel
    relationship: str
    object: str
    object_label: EntityLabel
    source: Optional[str] = None


class ErrorInfo(StrictModel):
    code: ErrorCode
    message: str


class GraphQAResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    question: str
    answer: str
    grounded: bool
    intent: Optional[IntentResult] = None
    linked_entities: List[LinkedEntity] = Field(default_factory=list)
    query: Optional[QueryInfo] = None
    evidence: List[Evidence] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    error: Optional[ErrorInfo] = None
    session_id: Optional[str] = None
    rewritten_question: Optional[str] = None


class GraphQAStreamEvent(StrictModel):
    event: Literal[
        "stage",
        "evidence",
        "answer_delta",
        "warning",
        "error",
        "done",
    ]
    data: Dict[str, Any] = Field(default_factory=dict)


def append_disclaimer(answer: str) -> str:
    """确保用户看到的医疗知识回答带统一提示。"""
    answer = answer.strip()
    if MEDICAL_DISCLAIMER in answer:
        return answer
    if not answer:
        return MEDICAL_DISCLAIMER
    return f"{answer}\n\n{MEDICAL_DISCLAIMER}"
