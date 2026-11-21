# -*- coding: utf-8 -*-
"""应用服务、Tool 契约和执行器测试。"""

import threading
from typing import Optional

from graph_qa.application.contracts import PathStepSpec
from graph_qa.application.entity_ids import entity_id_for
from graph_qa.application.graph_evidence_service import GraphEvidenceService
from graph_qa.entity_linker import EntityIndex, EntityRecord
from graph_qa.models import QueryResult, StrictModel
from graph_qa.schema import GraphSchema
from graph_qa.tools.catalog.graph_evidence import (
    TOOL_VERSION,
    build_tool_registry,
)
from graph_qa.tools.contracts import (
    ToolDefinition,
    ToolErrorCode,
    ToolResult,
    ToolSpec,
)
from graph_qa.tools.executor import ToolExecutor
from graph_qa.tools.registry import ToolRegistry


def make_schema() -> GraphSchema:
    properties = {
        "Herb": [
            "name",
            "alias",
            "property_flavor",
            "meridian",
        ],
        "Formula": ["name", "effect", "usage"],
    }
    return GraphSchema.from_mapping(
        {
            "format_version": "1.1",
            "node_labels": [
                {
                    "label": label,
                    "properties": [
                        {"name": property_name}
                        for property_name in properties.get(label, ["name"])
                    ],
                }
                for label in (
                    "Herb",
                    "Formula",
                    "Disease",
                    "Symptom",
                    "Effect",
                    "Source",
                )
            ],
            "relationship_types": [
                {"type": "HAS_EFFECT"},
                {"type": "HAS_INGREDIENT"},
                {"type": "FROM_SOURCE"},
            ],
            "triple_patterns": [
                {
                    "subject_label": "Herb",
                    "relationship_type": "HAS_EFFECT",
                    "object_label": "Effect",
                },
                {
                    "subject_label": "Formula",
                    "relationship_type": "HAS_INGREDIENT",
                    "object_label": "Herb",
                },
                {
                    "subject_label": "Herb",
                    "relationship_type": "FROM_SOURCE",
                    "object_label": "Source",
                },
            ],
        }
    )


def make_index() -> EntityIndex:
    return EntityIndex.from_records(
        [
            EntityRecord(label="Herb", name="麻黄", alias="龙沙"),
            EntityRecord(label="Formula", name="麻黄汤"),
            EntityRecord(label="Effect", name="发汗"),
            EntityRecord(label="Source", name="《古籍》"),
        ]
    )


class FakeRepository:
    max_results = 50

    def __init__(self, neighbor_rows: Optional[list[dict]] = None):
        self.index = make_index()
        self.attribute_plans = []
        self.neighbor_plans = []
        self.multi_hop_plans = []
        self.neighbor_rows = neighbor_rows

    def load_entity_index(self) -> EntityIndex:
        return self.index

    def execute_attributes(self, plan) -> QueryResult:
        self.attribute_plans.append(plan)
        rows = [
            {
                "subject": "麻黄",
                "subject_label": "Herb",
                "properties": {
                    "property_flavor": "辛、微苦，温",
                    "meridian": "归肺经、膀胱经",
                },
                "sources": ["《古籍》"],
            }
        ]
        return QueryResult(
            cypher="SECRET MATCH (subject) RETURN properties(subject)",
            parameters={"entity": "麻黄"},
            rows=rows,
            row_count=len(rows),
            elapsed_ms=1,
            limit_applied=50,
        )

    def execute_neighbors(self, plan) -> QueryResult:
        self.neighbor_plans.append(plan)
        rows = (
            self.neighbor_rows
            if self.neighbor_rows is not None
            else [
                {
                    "subject": "麻黄",
                    "subject_label": "Herb",
                    "relationship": "HAS_EFFECT",
                    "object": "发汗",
                    "object_label": "Effect",
                    "source": "《古籍》",
                }
            ]
        )
        return QueryResult(
            cypher="SECRET MATCH (subject)-[relationship]->(object)",
            parameters={"entity": "麻黄"},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=plan.limit,
        )

    def execute_multi_hop(self, plan) -> QueryResult:
        self.multi_hop_plans.append(plan)
        rows = [
            {
                "root": "麻黄汤",
                "root_label": "Formula",
                "relationship_types": [
                    "HAS_INGREDIENT",
                    "HAS_EFFECT",
                ],
                "node_names": ["麻黄", "发汗"],
                "node_labels": ["Herb", "Effect"],
                "target": "发汗",
                "target_label": "Effect",
                "sources": ["《古籍》"],
            }
        ]
        return QueryResult(
            cypher="SECRET MATCH path",
            parameters={"entity": "麻黄汤"},
            rows=rows,
            row_count=1,
            elapsed_ms=1,
            limit_applied=20,
        )


def make_service() -> tuple[GraphEvidenceService, FakeRepository]:
    repository = FakeRepository()
    service = GraphEvidenceService(
        repository=repository,
        schema=make_schema(),
        entity_index=repository.index,
    )
    return service, repository


class EmptyInput(StrictModel):
    pass


class ToolTestOutput(StrictModel):
    value: str


def make_definition(
    tool_id: str,
    *,
    version: str = "1.0.0",
    timeout_seconds: float = 1,
    max_rows: int = 10,
) -> ToolDefinition:
    return ToolDefinition(
        tool_id=tool_id,
        version=version,
        description="测试工具",
        input_schema=EmptyInput.model_json_schema(),
        output_schema=ToolTestOutput.model_json_schema(),
        timeout_seconds=timeout_seconds,
        max_rows=max_rows,
        scopes=["graph:read"],
        audit_policy="read_only",
    )


def test_tool_registry_lists_four_versioned_read_only_tools():
    service, _repository = make_service()

    registry = build_tool_registry(service)
    definitions = registry.list_definitions()

    assert [item.tool_id for item in definitions] == [
        "find_evidence_paths",
        "query_entity_facts",
        "query_neighbors",
        "resolve_entity",
    ]
    assert all(item.version == TOOL_VERSION for item in definitions)
    assert all(item.scopes == ["graph:read"] for item in definitions)
    assert all("input_schema" in item.model_dump() for item in definitions)


def test_tool_registry_rejects_duplicate_tool_version_and_selects_latest():
    registry = ToolRegistry()

    def handler(payload, context):
        return ToolResult(
            run_id=context.run_id,
            data=ToolTestOutput(value="ok"),
        )

    registry.register(
        ToolSpec(
            definition=make_definition("sample_tool", version="1.0.0"),
            input_model=EmptyInput,
            handler=handler,
        )
    )
    registry.register(
        ToolSpec(
            definition=make_definition("sample_tool", version="1.2.0"),
            input_model=EmptyInput,
            handler=handler,
        )
    )

    assert registry.get("sample_tool").definition.version == "1.2.0"
    try:
        registry.register(
            ToolSpec(
                definition=make_definition(
                    "sample_tool",
                    version="1.2.0",
                ),
                input_model=EmptyInput,
                handler=handler,
            )
        )
    except ValueError as exc:
        assert "已注册" in str(exc)
    else:
        raise AssertionError("重复 Tool 版本必须被拒绝")


def test_resolve_entity_tool_returns_entity_ref_without_raw_query():
    service, _repository = make_service()
    registry = build_tool_registry(service)

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "resolve_entity",
            {"query": "龙沙", "suggested_label": "Herb"},
        )

    assert result.error is None
    assert result.data is not None
    assert result.data.entities[0].entity.name == "麻黄"
    assert result.data.entities[0].entity.entity_id == entity_id_for(
        "Herb",
        "麻黄",
    )
    assert "cypher" not in result.model_dump_json().lower()


def test_service_can_reuse_existing_engine_dependencies():
    repository = FakeRepository()
    schema = make_schema()

    class FakeEngine:
        pass

    engine = FakeEngine()
    engine.repository = repository
    engine.schema = schema
    engine.entity_index = repository.index

    service = GraphEvidenceService.from_engine(engine)

    assert service.repository is repository
    assert service.schema is schema
    assert service.entity_index is repository.index


def test_query_entity_facts_tool_returns_facts_and_source_status():
    service, repository = make_service()
    registry = build_tool_registry(service)
    entity_id = entity_id_for("Herb", "麻黄")

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "query_entity_facts",
            {"entity_id": entity_id, "attributes": ["property_flavor"]},
        )

    assert result.error is None
    assert result.data is not None
    assert result.data.facts[0].attribute == "property_flavor"
    assert result.data.facts[0].source == "《古籍》"
    assert repository.attribute_plans[0].selections[0].attributes == [
        "property_flavor"
    ]
    assert "SECRET" not in result.model_dump_json()


def test_query_entity_facts_rejects_unknown_attribute_without_query():
    service, repository = make_service()
    registry = build_tool_registry(service)
    entity_id = entity_id_for("Herb", "麻黄")

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "query_entity_facts",
            {"entity_id": entity_id, "attributes": ["password"]},
        )

    assert result.error.code == ToolErrorCode.INVALID_ARGUMENT
    assert repository.attribute_plans == []


def test_query_neighbors_tool_returns_direction_and_evidence_only():
    service, repository = make_service()
    registry = build_tool_registry(service)
    entity_id = entity_id_for("Herb", "麻黄")

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "query_neighbors",
            {
                "entity_id": entity_id,
                "relationship_types": ["HAS_EFFECT"],
                "direction": "outgoing",
                "limit": 5,
            },
        )

    assert result.error is None
    assert result.data is not None
    assert result.data.neighbors[0].direction == "outgoing"
    assert result.data.neighbors[0].entity.name == "发汗"
    assert result.evidence[0].relationship == "HAS_EFFECT"
    assert repository.neighbor_plans[0].limit == 5
    assert "SECRET" not in result.model_dump_json()


def test_query_neighbors_supports_incoming_direction():
    repository = FakeRepository(
        neighbor_rows=[
            {
                "subject": "麻黄汤",
                "subject_label": "Formula",
                "relationship": "HAS_INGREDIENT",
                "object": "麻黄",
                "object_label": "Herb",
                "source": "《古籍》",
            }
        ]
    )
    service = GraphEvidenceService(
        repository=repository,
        schema=make_schema(),
        entity_index=repository.index,
    )
    registry = build_tool_registry(service)
    entity_id = entity_id_for("Herb", "麻黄")

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "query_neighbors",
            {
                "entity_id": entity_id,
                "relationship_types": ["HAS_INGREDIENT"],
                "direction": "incoming",
            },
        )

    assert result.error is None
    assert result.data.neighbors[0].direction == "incoming"
    assert result.data.neighbors[0].entity.name == "麻黄汤"
    assert repository.neighbor_plans[0].direction == "incoming"


def test_find_evidence_paths_tool_returns_path_dto():
    service, repository = make_service()
    registry = build_tool_registry(service)
    entity_id = entity_id_for("Formula", "麻黄汤")

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "find_evidence_paths",
            {
                "entity_id": entity_id,
                "steps": [
                    {
                        "relationship": "HAS_INGREDIENT",
                        "object_label": "Herb",
                    },
                    {
                        "relationship": "HAS_EFFECT",
                        "object_label": "Effect",
                    },
                ],
            },
        )

    assert result.error is None
    assert result.data is not None
    assert result.data.paths[0].root == "麻黄汤"
    assert result.data.paths[0].target == "发汗"
    assert result.data.paths[0].source == "《古籍》"
    assert len(repository.multi_hop_plans) == 1
    assert "SECRET" not in result.model_dump_json()


def test_invalid_path_relation_returns_stable_error_without_query():
    service, repository = make_service()
    registry = build_tool_registry(service)
    entity_id = entity_id_for("Formula", "麻黄汤")

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "find_evidence_paths",
            {
                "entity_id": entity_id,
                "steps": [
                    {
                        "relationship": "UNKNOWN_REL",
                        "object_label": "Herb",
                    },
                    {
                        "relationship": "HAS_EFFECT",
                        "object_label": "Effect",
                    },
                ],
            },
        )

    assert result.error is not None
    assert result.error.code == ToolErrorCode.INVALID_ARGUMENT
    assert repository.multi_hop_plans == []


def test_unknown_tool_and_invalid_arguments_return_structured_errors():
    service, _repository = make_service()
    registry = build_tool_registry(service)

    with ToolExecutor(registry) as executor:
        unknown = executor.execute("missing_tool", {})
        invalid = executor.execute("resolve_entity", {"query": ""})

    assert unknown.error.code == ToolErrorCode.TOOL_NOT_FOUND
    assert invalid.error.code == ToolErrorCode.INVALID_ARGUMENT
    assert invalid.data is None
    assert invalid.error.details


def test_unknown_entity_id_returns_stable_tool_error():
    service, _repository = make_service()
    registry = build_tool_registry(service)

    with ToolExecutor(registry) as executor:
        result = executor.execute(
            "query_entity_facts",
            {"entity_id": "eid_" + "0" * 32},
        )

    assert result.error.code == ToolErrorCode.ENTITY_NOT_FOUND
    assert result.data is None


def test_tool_executor_enforces_timeout_and_result_limit():
    release = threading.Event()

    def slow_handler(payload, context):
        release.wait(timeout=1)
        return ToolResult(
            run_id=context.run_id,
            data=ToolTestOutput(value="late"),
        )

    def oversized_handler(payload, context):
        return ToolResult(
            run_id=context.run_id,
            data=OversizedOutput(
                items=[
                    ToolTestOutput(value="1"),
                    ToolTestOutput(value="2"),
                ]
            ),
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            definition=make_definition(
                "slow_tool",
                timeout_seconds=0.01,
            ),
            input_model=EmptyInput,
            handler=slow_handler,
        )
    )
    registry.register(
        ToolSpec(
            definition=make_definition("oversized_tool", max_rows=1),
            input_model=EmptyInput,
            handler=oversized_handler,
        )
    )
    with ToolExecutor(registry) as executor:
        timeout_result = executor.execute("slow_tool", {})
        oversized_result = executor.execute("oversized_tool", {})
    release.set()

    assert timeout_result.error.code == ToolErrorCode.TOOL_TIMEOUT
    assert oversized_result.error.code == ToolErrorCode.INTERNAL_ERROR


class OversizedOutput(StrictModel):
    items: list[ToolTestOutput]
