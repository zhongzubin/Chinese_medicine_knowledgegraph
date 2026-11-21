# -*- coding: utf-8 -*-
"""只读 Tool 的公共契约。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Type,
    TypeVar,
)

from pydantic import BaseModel, Field

from graph_qa.models import Evidence, StrictModel


class ToolErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    QUERY_EXECUTION_FAILED = "QUERY_EXECUTION_FAILED"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ToolErrorDetail(StrictModel):
    location: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=160)


class ToolError(StrictModel):
    code: ToolErrorCode
    message: str = Field(min_length=1, max_length=300)
    details: List[ToolErrorDetail] = Field(default_factory=list)


class ToolDefinition(StrictModel):
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=300)
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    timeout_seconds: float = Field(gt=0)
    max_rows: int = Field(ge=1)
    scopes: List[str] = Field(min_length=1)
    audit_policy: str = Field(min_length=1, max_length=80)


ToolDataT = TypeVar("ToolDataT", bound=BaseModel)


class ToolResult(StrictModel, Generic[ToolDataT]):
    data: Optional[ToolDataT] = None
    evidence: List[Evidence] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    run_id: str = Field(min_length=1)
    degraded: bool = False
    error: Optional[ToolError] = None


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    timeout_seconds: float
    max_rows: int


ToolHandler = Callable[
    [BaseModel, ToolExecutionContext],
    ToolResult[Any],
]


@dataclass(frozen=True)
class ToolSpec:
    definition: ToolDefinition
    input_model: Type[BaseModel]
    handler: ToolHandler = field(repr=False)
