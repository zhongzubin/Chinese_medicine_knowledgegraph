# -*- coding: utf-8 -*-
"""为 Web、Tool 和后续 MCP 提供统一的图谱证据应用服务。"""

from typing import Any, List, Optional, Protocol, Sequence, Tuple

from graph_qa.application.contracts import (
    EntityFactsData,
    EntityNeighborsData,
    EntityRef,
    EvidencePathsData,
    GraphEvidenceError,
    GraphEvidenceErrorCode,
    NeighborEdge,
    PathStepSpec,
    ResolvedEntity,
    ResolveEntityData,
)
from graph_qa.application.entity_ids import EntityIdMapper
from graph_qa.entity_linker import EntityIndex
from graph_qa.models import (
    AttributeQueryPlan,
    AttributeSelection,
    EntityCandidate,
    EntityLabel,
    ErrorCode,
    Evidence,
    MultiHopQueryPlan,
    MultiHopStep,
    NeighborDirection,
    NeighborQueryPlan,
    PathEvidence,
    PathStepEvidence,
    QueryResult,
    UsedEntityRef,
)
from graph_qa.neo4j_repository import RepositoryError
from graph_qa.schema import GraphSchema
from graph_qa.workflow import evidence_from_rows, facts_from_rows


class GraphReadRepository(Protocol):
    max_results: int

    def load_entity_index(self) -> EntityIndex:
        ...

    def execute_attributes(
        self,
        plan: AttributeQueryPlan,
    ) -> QueryResult:
        ...

    def execute_neighbors(
        self,
        plan: NeighborQueryPlan,
    ) -> QueryResult:
        ...

    def execute_multi_hop(
        self,
        plan: MultiHopQueryPlan,
    ) -> QueryResult:
        ...


class GraphEvidenceService:
    def __init__(
        self,
        repository: GraphReadRepository,
        schema: GraphSchema,
        entity_index: Optional[EntityIndex] = None,
    ):
        self.repository = repository
        self.schema = schema
        self.entity_index = entity_index or repository.load_entity_index()
        self.entity_ids = EntityIdMapper(self.entity_index.entries)

    @classmethod
    def from_engine(cls, engine: Any) -> "GraphEvidenceService":
        """复用现有 GraphQAEngine 的只读依赖，不改变其工作流。"""
        return cls(
            repository=engine.repository,
            schema=engine.schema,
            entity_index=engine.entity_index,
        )

    def require_entity(self, entity_id: str) -> EntityRef:
        entity = self.entity_ids.resolve(entity_id)
        if entity is None:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.ENTITY_NOT_FOUND,
                "未找到对应实体",
            )
        return entity

    def resolve_entity(
        self,
        query: str,
        suggested_label: Optional[EntityLabel] = None,
        limit: int = 10,
    ) -> ResolveEntityData:
        query = query.strip()
        if not query:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "实体查询不能为空",
            )
        if limit < 1:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "实体查询数量必须大于等于 1",
            )
        matches = self.entity_index.match(
            [
                EntityCandidate(
                    text=query,
                    suggested_label=suggested_label,
                )
            ],
            query,
            limit_per_label=limit,
        )
        return ResolveEntityData(
            entities=[
                ResolvedEntity(
                    entity=self.entity_ids.entity_ref(
                        match.label,
                        match.name,
                    ),
                    matched_text=match.matched_text,
                    match_type=match.match_type,
                    score=match.score,
                )
                for match in matches
            ]
        )

    def query_entity_facts(
        self,
        entity_id: str,
        attributes: Sequence[str] = (),
    ) -> EntityFactsData:
        entity = self.require_entity(entity_id)
        available_attributes = list(
            self.schema.queryable_attributes(entity.label)
        )
        requested_attributes = [
            attribute.strip()
            for attribute in attributes
            if attribute.strip()
        ]
        invalid_attributes = (
            set(requested_attributes) - set(available_attributes)
        )
        if invalid_attributes:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "属性查询包含不允许的字段",
            )
        selected_attributes = (
            requested_attributes or available_attributes
        )
        if not selected_attributes:
            return EntityFactsData(
                entity=entity,
                available_attributes=[],
                facts=[],
            )

        plan = AttributeQueryPlan(
            used_entities=[
                UsedEntityRef(label=entity.label, name=entity.name)
            ],
            selections=[
                AttributeSelection(
                    label=entity.label,
                    attributes=selected_attributes,
                )
            ],
        )
        try:
            result = self.repository.execute_attributes(plan)
        except RepositoryError as exc:
            self._raise_repository_error(exc)
        return EntityFactsData(
            entity=entity,
            available_attributes=available_attributes,
            facts=facts_from_rows(result.rows, plan),
        )

    def query_neighbors(
        self,
        entity_id: str,
        relationship_types: Sequence[str] = (),
        direction: NeighborDirection = "both",
        limit: int = 20,
    ) -> Tuple[EntityNeighborsData, List[Evidence]]:
        entity = self.require_entity(entity_id)
        if direction not in {"outgoing", "incoming", "both"}:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "邻居查询方向无效",
            )
        if limit < 1:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "邻居查询数量必须大于等于 1",
            )
        requested_relationships = [
            relationship.strip()
            for relationship in relationship_types
            if relationship.strip()
        ]
        invalid_relationships = (
            set(requested_relationships)
            - self.schema.relationship_types
        )
        if invalid_relationships:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "邻居查询包含不允许的关系类型",
            )
        plan = NeighborQueryPlan(
            entity=UsedEntityRef(label=entity.label, name=entity.name),
            direction=direction,
            relationship_types=requested_relationships,
            limit=limit,
        )
        try:
            result = self.repository.execute_neighbors(plan)
        except RepositoryError as exc:
            self._raise_repository_error(exc)

        evidence = evidence_from_rows(result.rows, self.schema)
        anchor_key = (entity.label, entity.name)
        neighbors_by_key = {}
        for item in evidence:
            subject_key = (item.subject_label, item.subject)
            object_key = (item.object_label, item.object)
            if subject_key == anchor_key:
                edge_direction = "outgoing"
                neighbor_label = item.object_label
                neighbor_name = item.object
            elif object_key == anchor_key:
                edge_direction = "incoming"
                neighbor_label = item.subject_label
                neighbor_name = item.subject
            else:
                continue
            key = (
                neighbor_label,
                neighbor_name,
                edge_direction,
                item.relationship,
            )
            if key in neighbors_by_key:
                continue
            neighbors_by_key[key] = NeighborEdge(
                entity=self.entity_ids.entity_ref(
                    neighbor_label,
                    neighbor_name,
                ),
                direction=edge_direction,
                relationship=item.relationship,
                source=item.source,
            )
        return (
            EntityNeighborsData(
                entity=entity,
                neighbors=list(neighbors_by_key.values()),
            ),
            evidence,
        )

    def find_evidence_paths(
        self,
        entity_id: str,
        steps: Sequence[PathStepSpec],
    ) -> EvidencePathsData:
        entity = self.require_entity(entity_id)
        if not 2 <= len(steps) <= 3:
            raise GraphEvidenceError(
                GraphEvidenceErrorCode.INVALID_ARGUMENT,
                "多跳路径必须包含 2 到 3 步",
            )

        current_label = entity.label
        visited_labels = {current_label}
        used_relationships = set()
        query_steps = []
        for step in steps:
            relationship = step.relationship.strip()
            if relationship in used_relationships:
                raise GraphEvidenceError(
                    GraphEvidenceErrorCode.INVALID_ARGUMENT,
                    "多跳路径不能重复使用关系类型",
                )
            if step.object_label in visited_labels:
                raise GraphEvidenceError(
                    GraphEvidenceErrorCode.INVALID_ARGUMENT,
                    "多跳路径不能重复经过相同实体标签",
                )
            try:
                self.schema.assert_pattern(
                    current_label,
                    relationship,
                    step.object_label,
                )
            except Exception as exc:
                raise GraphEvidenceError(
                    GraphEvidenceErrorCode.INVALID_ARGUMENT,
                    "多跳路径不符合图谱 Schema",
                ) from exc
            query_steps.append(
                MultiHopStep(
                    subject_label=current_label,
                    relationship=relationship,
                    object_label=step.object_label,
                )
            )
            current_label = step.object_label
            visited_labels.add(current_label)
            used_relationships.add(relationship)

        plan = MultiHopQueryPlan(
            steps=query_steps,
            used_entities=[
                UsedEntityRef(label=entity.label, name=entity.name)
            ],
            description=f"{entity.name}的受控{len(query_steps)}步路径",
        )
        try:
            result = self.repository.execute_multi_hop(plan)
        except RepositoryError as exc:
            self._raise_repository_error(exc)
        return EvidencePathsData(
            entity=entity,
            paths=self._paths_from_rows(result.rows, plan),
        )

    @staticmethod
    def _raise_repository_error(exc: RepositoryError) -> None:
        code = (
            GraphEvidenceErrorCode.QUERY_TIMEOUT
            if exc.code in {
                ErrorCode.QUERY_TIMEOUT,
            }
            else GraphEvidenceErrorCode.QUERY_EXECUTION_FAILED
        )
        raise GraphEvidenceError(code, exc.message) from exc

    @staticmethod
    def _paths_from_rows(
        rows: Sequence[dict],
        plan: MultiHopQueryPlan,
    ) -> List[PathEvidence]:
        paths = []
        seen_paths = set()
        for row in rows:
            root = row.get("root")
            root_label = row.get("root_label")
            target = row.get("target")
            target_label = row.get("target_label")
            relationships = row.get("relationship_types")
            node_names = row.get("node_names")
            node_labels = row.get("node_labels")
            if (
                not isinstance(root, str)
                or not isinstance(target, str)
                or not isinstance(relationships, list)
                or not isinstance(node_names, list)
                or not isinstance(node_labels, list)
                or len(relationships) != len(plan.steps)
                or len(node_names) != len(plan.steps)
                or len(node_labels) != len(plan.steps)
                or root_label != plan.steps[0].subject_label
                or target_label != plan.steps[-1].object_label
            ):
                continue
            expected_relationships = [
                step.relationship for step in plan.steps
            ]
            expected_node_labels = [
                step.object_label for step in plan.steps
            ]
            if (
                relationships != expected_relationships
                or node_labels != expected_node_labels
                or any(not isinstance(name, str) for name in node_names)
                or target != node_names[-1]
            ):
                continue
            sources = row.get("sources")
            source = None
            if isinstance(sources, list):
                source_names = [
                    item.strip()
                    for item in sources
                    if isinstance(item, str) and item.strip()
                ]
                source = "；".join(source_names) or None
            path_steps = []
            subject = root
            subject_label = root_label
            for step, object_name, object_label in zip(
                plan.steps,
                node_names,
                node_labels,
                strict=True,
            ):
                path_steps.append(
                    PathStepEvidence(
                        subject=subject,
                        subject_label=subject_label,
                        relationship=step.relationship,
                        object=object_name,
                        object_label=object_label,
                    )
                )
                subject = object_name
                subject_label = object_label
            key = (
                root,
                tuple(node_names),
                tuple(relationships),
                source,
            )
            if key in seen_paths:
                continue
            seen_paths.add(key)
            paths.append(
                PathEvidence(
                    root=root,
                    root_label=root_label,
                    target=target,
                    target_label=target_label,
                    source=source,
                    steps=path_steps,
                )
            )
        return paths
