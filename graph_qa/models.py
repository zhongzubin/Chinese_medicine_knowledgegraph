# -*- coding: utf-8 -*-
"""知识图谱问答的数据模型和稳定性约定。"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EntityLabel = Literal["Herb", "Formula", "Disease", "Symptom", "Effect", "Source"]  # 实体标签
MatchType = Literal["name", "alias", "fuzzy", "semantic"]  # 匹配类型
AggregateDirection = Literal["outgoing", "incoming"]  # 聚合方向
RetrievalPlanKind = Literal[
    "controlled_multi_hop", # 控制的多跳查询
    "controlled_attribute",  # 控制的属性查询
    "controlled_aggregate",  # 控制的聚合查询
    "controlled_multi_condition_list",  # 控制的多条件列表查询
    "controlled_relationship",  # 控制的关系查询
    "schema_constrained_cypher",  # 模式约束的Cypher查询
    "rejected",  # 拒绝的查询
]

MEDICAL_DISCLAIMER = "以上内容仅供中医药知识参考，不能替代专业医疗诊断和治疗。"


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"     # 请求无效
    UNSAFE_REQUEST = "UNSAFE_REQUEST"     # 请求不安全
    UNAUTHORIZED = "UNAUTHORIZED"     # 未授权
    RATE_LIMITED = "RATE_LIMITED"     # 请求频率限制
    SERVICE_BUSY = "SERVICE_BUSY"     # 服务繁忙
    RATE_LIMIT_UNAVAILABLE = "RATE_LIMIT_UNAVAILABLE"     # 请求频率限制不可用
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"     # 会话未找到
    SESSION_BUSY = "SESSION_BUSY"     # 会话繁忙
    SESSION_UNAVAILABLE = "SESSION_UNAVAILABLE"     # 会话不可用
    CONFIG_ERROR = "CONFIG_ERROR"     # 配置错误
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"     # 模型不可用
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"     # 模型输出无效
    CYPHER_REJECTED = "CYPHER_REJECTED"     # Cypher查询被拒绝
    CYPHER_INVALID = "CYPHER_INVALID"     # Cypher查询无效
    QUERY_TIMEOUT = "QUERY_TIMEOUT"     # 查询超时
    QUERY_EXECUTION_FAILED = "QUERY_EXECUTION_FAILED"     # 查询执行失败
    NO_GRAPH_EVIDENCE = "NO_GRAPH_EVIDENCE"     # 无图谱证据
    INTERNAL_ERROR = "INTERNAL_ERROR"     # 内部错误


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


class AttributeSelection(StrictModel):
    label: EntityLabel
    attributes: List[str] = Field(min_length=1)

    @field_validator("attributes")
    @classmethod
    def normalize_attributes(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("属性名不能为空")
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("至少需要一个属性")
        return normalized


class AttributeQueryPlan(StrictModel):
    used_entities: List[UsedEntityRef] = Field(min_length=1)
    selections: List[AttributeSelection] = Field(min_length=1)
    expected_result_shape: Literal["attribute_values"] = "attribute_values"


class AggregateQueryPlan(StrictModel):
    subject_label: EntityLabel
    relationship: str = Field(min_length=1, max_length=80)
    object_label: EntityLabel
    used_entities: List[UsedEntityRef] = Field(min_length=1)
    direction: AggregateDirection
    require_all_entities: bool = False
    description: str = Field(min_length=1, max_length=160)


class MultiConditionQueryPlan(StrictModel):
    subject_label: EntityLabel
    relationship: str = Field(min_length=1, max_length=80)
    object_label: EntityLabel
    used_entities: List[UsedEntityRef] = Field(min_length=2)
    description: str = Field(min_length=1, max_length=160)


class MultiHopStep(StrictModel):
    subject_label: EntityLabel
    relationship: str = Field(min_length=1, max_length=80)
    object_label: EntityLabel


class MultiHopQueryPlan(StrictModel):
    steps: List[MultiHopStep] = Field(min_length=2, max_length=2)
    used_entities: List[UsedEntityRef] = Field(min_length=1)
    description: str = Field(min_length=1, max_length=160)


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


class RetrievalPlanningInput(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    linked_entities: List[LinkedEntity] = Field(min_length=1)
    validation_errors: List[str] = Field(default_factory=list)
    attribute_query_attempted: bool = False
    query_attempts: int = Field(default=0, ge=0)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("检索规划问题不能为空")
        return value


class RetrievalPlanningResult(StrictModel):
    plan_kind: RetrievalPlanKind
    plan: Optional[
        Union[
            AttributeQueryPlan,
            AggregateQueryPlan,
            MultiConditionQueryPlan,
            MultiHopQueryPlan,
            CypherPlan,
        ]
    ] = None
    query_attempts: int = Field(default=0, ge=0)
    validation_errors: List[str] = Field(default_factory=list)
    last_error_code: Optional[ErrorCode] = None

    @model_validator(mode="after")
    def validate_plan_payload(self) -> "RetrievalPlanningResult":
        expected_plan_type = {
            "controlled_multi_hop": MultiHopQueryPlan,
            "controlled_attribute": AttributeQueryPlan,
            "controlled_aggregate": AggregateQueryPlan,
            "controlled_multi_condition_list": MultiConditionQueryPlan,
            "controlled_relationship": CypherPlan,
            "schema_constrained_cypher": CypherPlan,
        }.get(self.plan_kind)
        if expected_plan_type is None:
            if self.plan is not None:
                raise ValueError("拒绝的检索计划不能携带查询计划")
            return self
        if not isinstance(self.plan, expected_plan_type):
            raise ValueError(
                f"{self.plan_kind} 必须携带 {expected_plan_type.__name__}"
            )
        return self

# 查询信息
class QueryInfo(StrictModel):
    cypher: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    plan_kind: Optional[RetrievalPlanKind] = None


# 查询结果
class QueryResult(StrictModel):
    cypher: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    limit_applied: int = Field(ge=1)

# 证据
class Evidence(StrictModel):
    subject: str
    subject_label: EntityLabel
    relationship: str
    object: str
    object_label: EntityLabel
    source: Optional[str] = None

# 属性事实
class AttributeFact(StrictModel):
    subject: str = Field(min_length=1, max_length=100)
    subject_label: EntityLabel
    attribute: str = Field(min_length=1, max_length=80)
    attribute_label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=4000)
    source: Optional[str] = Field(default=None, max_length=1000)

# 聚合事实

class AggregateFact(StrictModel):
    anchor: str = Field(min_length=1, max_length=100)
    anchor_label: EntityLabel
    relationship: str = Field(min_length=1, max_length=80)
    result_label: EntityLabel
    direction: AggregateDirection
    count: int = Field(ge=0)
    description: str = Field(min_length=1, max_length=160)

# 路径证据
class PathEvidence(StrictModel):
    root: str = Field(min_length=1, max_length=100)
    root_label: EntityLabel
    first_relationship: str = Field(min_length=1, max_length=80)
    intermediate: str = Field(min_length=1, max_length=100)
    intermediate_label: EntityLabel
    second_relationship: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=100)
    target_label: EntityLabel
    source: Optional[str] = Field(default=None, max_length=1000)

# 工作流阶段耗时
class StageTiming(StrictModel):
    stage: str = Field(min_length=1, max_length=80)
    elapsed_ms: float = Field(ge=0)

# 回答断言
class AnswerClaim(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    support_ids: List[str] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("回答断言不能为空")
        return value

    @field_validator("support_ids")
    @classmethod
    def normalize_support_ids(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if value and value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ValueError("回答断言至少需要一个证据编号")
        return normalized


# 结构化图谱回答
class GraphAnswerDraft(StrictModel):
    claims: List[AnswerClaim] = Field(min_length=1, max_length=50)

# 答案验证
class AnswerVerification(StrictModel):
    status: Literal["passed", "fallback"]
    agent: Literal["evidence_audit"] = "evidence_audit"
    checked_items: int = Field(ge=0)
    matched_items: int = Field(ge=0)
    evidence_items: int = Field(ge=0)
    fact_items: int = Field(ge=0)
    aggregate_items: int = Field(ge=0)
    path_items: int = Field(ge=0)
    missing_sources: int = Field(ge=0)
    fallback_used: bool
    message: str = Field(min_length=1, max_length=300)

# 错误信息
class ErrorInfo(StrictModel):
    code: ErrorCode
    message: str

# 图谱查询响应
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
    facts: List[AttributeFact] = Field(default_factory=list)
    aggregates: List[AggregateFact] = Field(default_factory=list)
    paths: List[PathEvidence] = Field(default_factory=list)
    verification: Optional[AnswerVerification] = None
    warnings: List[str] = Field(default_factory=list)
    error: Optional[ErrorInfo] = None
    session_id: Optional[str] = None
    rewritten_question: Optional[str] = None


# 图谱查询流事件
class GraphQAStreamEvent(StrictModel):
    event: Literal[
        "stage",
        "evidence",
        "facts",
        "aggregates",
        "paths",
        "audit",
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
