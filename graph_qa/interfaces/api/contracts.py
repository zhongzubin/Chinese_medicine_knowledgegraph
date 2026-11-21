# -*- coding: utf-8 -*-
"""V2 REST API 请求、响应包络和页面 DTO。"""

from typing import Generic, List, Literal, Optional, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from graph_qa.application.contracts import (
    EntityRef,
    PathStepSpec,
)
from graph_qa.models import EntityLabel, Evidence, StrictModel
from graph_qa.tools.contracts import ToolDefinition


class ApiError(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)


class ApiWarning(StrictModel):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=300)


class ResponseMeta(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    tool_version: Optional[str] = None
    skill_version: Optional[str] = None
    duration_ms: int = Field(ge=0)
    degraded: bool = False


ApiDataT = TypeVar("ApiDataT", bound=BaseModel)


class ApiEnvelope(StrictModel, Generic[ApiDataT]):
    request_id: str = Field(min_length=1)
    run_id: Optional[str] = None
    data: Optional[ApiDataT] = None
    evidence: List[Evidence] = Field(default_factory=list)
    warnings: List[ApiWarning] = Field(default_factory=list)
    error: Optional[ApiError] = None
    meta: ResponseMeta


class V2QARequest(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    session_id: Optional[UUID] = None

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value


class EntitySearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=100)
    suggested_label: Optional[EntityLabel] = None
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def strip_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("实体查询不能为空")
        return value


class EvidencePathRequest(StrictModel):
    entity_id: str = Field(pattern=r"^eid_[0-9a-f]{32}$")
    steps: List[PathStepSpec] = Field(min_length=2, max_length=3)


class EntityDetailData(StrictModel):
    entity: EntityRef
    available_attributes: List[str] = Field(default_factory=list)


class ToolCatalogData(StrictModel):
    tools: List[ToolDefinition] = Field(default_factory=list)


class SchemaPatternData(StrictModel):
    subject_label: EntityLabel
    relationship: str
    object_label: EntityLabel


class SchemaSummaryData(StrictModel):
    format_version: str
    entity_labels: List[EntityLabel] = Field(default_factory=list)
    relationship_types: List[str] = Field(default_factory=list)
    triple_patterns: List[SchemaPatternData] = Field(default_factory=list)
