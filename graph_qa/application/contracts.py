# -*- coding: utf-8 -*-
"""应用服务对外使用的领域 DTO 和稳定错误。"""

from enum import Enum
from typing import List, Literal, Optional

from pydantic import Field

from graph_qa.models import (
    AttributeFact,
    EntityLabel,
    MatchType,
    PathEvidence,
    StrictModel,
)


class EntityRef(StrictModel):
    entity_id: str = Field(pattern=r"^eid_[0-9a-f]{32}$")
    label: EntityLabel
    name: str = Field(min_length=1, max_length=100)


class ResolvedEntity(StrictModel):
    entity: EntityRef
    matched_text: str = Field(min_length=1, max_length=100)
    match_type: MatchType
    score: float = Field(ge=0.0, le=1.0)


class ResolveEntityData(StrictModel):
    entities: List[ResolvedEntity] = Field(default_factory=list)


class EntityFactsData(StrictModel):
    entity: EntityRef
    available_attributes: List[str] = Field(default_factory=list)
    facts: List[AttributeFact] = Field(default_factory=list)


class NeighborEdge(StrictModel):
    entity: EntityRef
    direction: Literal["outgoing", "incoming"]
    relationship: str = Field(min_length=1, max_length=80)
    source: Optional[str] = None


class EntityNeighborsData(StrictModel):
    entity: EntityRef
    neighbors: List[NeighborEdge] = Field(default_factory=list)


class PathStepSpec(StrictModel):
    relationship: str = Field(min_length=1, max_length=80)
    object_label: EntityLabel


class EvidencePathsData(StrictModel):
    entity: EntityRef
    paths: List[PathEvidence] = Field(default_factory=list)


class GraphEvidenceErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    QUERY_EXECUTION_FAILED = "QUERY_EXECUTION_FAILED"


class GraphEvidenceError(RuntimeError):
    def __init__(self, code: GraphEvidenceErrorCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
