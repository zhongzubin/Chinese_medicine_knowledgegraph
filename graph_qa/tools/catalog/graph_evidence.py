# -*- coding: utf-8 -*-
"""图谱证据应用服务对应的只读 Tool 清单。"""

from functools import partial
from typing import List, Optional, Sequence

from pydantic import Field, field_validator

from graph_qa.application.contracts import (
    EntityFactsData,
    EntityNeighborsData,
    EvidencePathsData,
    PathStepSpec,
    ResolveEntityData,
)
from graph_qa.application.graph_evidence_service import GraphEvidenceService
from graph_qa.models import (
    EntityLabel,
    Evidence,
    NeighborDirection,
    StrictModel,
)
from graph_qa.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolSpec,
)
from graph_qa.tools.registry import ToolRegistry

TOOL_VERSION = "1.0.0"
TOOL_SCOPES = ["graph:read"]
TOOL_AUDIT_POLICY = "read_only_evidence"


class ResolveEntityInput(StrictModel):
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


class QueryEntityFactsInput(StrictModel):
    entity_id: str = Field(pattern=r"^eid_[0-9a-f]{32}$")
    attributes: List[str] = Field(default_factory=list)

    @field_validator("attributes")
    @classmethod
    def normalize_attributes(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if value and value not in normalized:
                normalized.append(value)
        return normalized


class QueryNeighborsInput(StrictModel):
    entity_id: str = Field(pattern=r"^eid_[0-9a-f]{32}$")
    relationship_types: List[str] = Field(default_factory=list)
    direction: NeighborDirection = "both"
    limit: int = Field(default=20, ge=1, le=50)

    @field_validator("relationship_types")
    @classmethod
    def normalize_relationships(cls, values: List[str]) -> List[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if value and value not in normalized:
                normalized.append(value)
        return normalized


class FindEvidencePathsInput(StrictModel):
    entity_id: str = Field(pattern=r"^eid_[0-9a-f]{32}$")
    steps: List[PathStepSpec] = Field(min_length=2, max_length=3)


def _definition(
    tool_id: str,
    description: str,
    input_model: type[StrictModel],
    output_model: type[StrictModel],
    timeout_seconds: float,
    max_rows: int,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        version=TOOL_VERSION,
        description=description,
        input_schema=input_model.model_json_schema(),
        output_schema=output_model.model_json_schema(),
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
        scopes=TOOL_SCOPES,
        audit_policy=TOOL_AUDIT_POLICY,
    )


def _result(
    context: ToolExecutionContext,
    data: StrictModel,
    evidence: Sequence[Evidence] = (),
    warnings: Sequence[str] = (),
    degraded: bool = False,
) -> ToolResult:
    return ToolResult(
        data=data,
        evidence=list(evidence),
        warnings=list(warnings),
        run_id=context.run_id,
        degraded=degraded,
    )


def _resolve_entity(
    service: GraphEvidenceService,
    payload: ResolveEntityInput,
    context: ToolExecutionContext,
) -> ToolResult[ResolveEntityData]:
    data = service.resolve_entity(
        payload.query,
        suggested_label=payload.suggested_label,
        limit=min(payload.limit, context.max_rows),
    )
    return _result(context, data)


def _query_entity_facts(
    service: GraphEvidenceService,
    payload: QueryEntityFactsInput,
    context: ToolExecutionContext,
) -> ToolResult[EntityFactsData]:
    data = service.query_entity_facts(
        payload.entity_id,
        attributes=payload.attributes,
    )
    return _result(
        context,
        data,
        warnings=(
            []
            if data.available_attributes
            else ["当前实体类型没有开放的属性字段"]
        ),
    )


def _query_neighbors(
    service: GraphEvidenceService,
    payload: QueryNeighborsInput,
    context: ToolExecutionContext,
) -> ToolResult[EntityNeighborsData]:
    data, evidence = service.query_neighbors(
        payload.entity_id,
        relationship_types=payload.relationship_types,
        direction=payload.direction,
        limit=min(payload.limit, context.max_rows),
    )
    return _result(context, data, evidence=evidence)


def _find_evidence_paths(
    service: GraphEvidenceService,
    payload: FindEvidencePathsInput,
    context: ToolExecutionContext,
) -> ToolResult[EvidencePathsData]:
    data = service.find_evidence_paths(
        payload.entity_id,
        steps=payload.steps,
    )
    return _result(context, data)


def build_tool_registry(
    service: GraphEvidenceService,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            definition=_definition(
                "resolve_entity",
                "按名称、别名或语义描述解析图谱实体",
                ResolveEntityInput,
                ResolveEntityData,
                timeout_seconds=4,
                max_rows=20,
            ),
            input_model=ResolveEntityInput,
            handler=partial(_resolve_entity, service),
        )
    )
    registry.register(
        ToolSpec(
            definition=_definition(
                "query_entity_facts",
                "查询实体白名单属性事实和来源",
                QueryEntityFactsInput,
                EntityFactsData,
                timeout_seconds=5,
                max_rows=50,
            ),
            input_model=QueryEntityFactsInput,
            handler=partial(_query_entity_facts, service),
        )
    )
    registry.register(
        ToolSpec(
            definition=_definition(
                "query_neighbors",
                "查询实体的一跳邻居、方向和来源",
                QueryNeighborsInput,
                EntityNeighborsData,
                timeout_seconds=5,
                max_rows=50,
            ),
            input_model=QueryNeighborsInput,
            handler=partial(_query_neighbors, service),
        )
    )
    registry.register(
        ToolSpec(
            definition=_definition(
                "find_evidence_paths",
                "按 Schema 受控关系序列查找 2～3 步证据路径",
                FindEvidencePathsInput,
                EvidencePathsData,
                timeout_seconds=8,
                max_rows=20,
            ),
            input_model=FindEvidencePathsInput,
            handler=partial(_find_evidence_paths, service),
        )
    )
    return registry
