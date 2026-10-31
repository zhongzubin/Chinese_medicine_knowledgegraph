# -*- coding: utf-8 -*-
"""Neo4j 只读仓储测试，不连接真实数据库。"""

from neo4j import READ_ACCESS
import pytest

from graph_qa.cypher_guard import CypherGuard, CypherGuardError
from graph_qa.models import (
    AggregateQueryPlan,
    AttributeQueryPlan,
    AttributeSelection,
    CypherPlan,
    MultiConditionQueryPlan,
    MultiHopQueryPlan,
    MultiHopStep,
    UsedEntityRef,
)
from graph_qa.neo4j_repository import (
    ENTITY_INDEX_QUERY,
    Neo4jRepository,
    RepositoryError,
)
from graph_qa.schema import GraphSchema

VALID_QUERY = """
MATCH (subject:Herb)-[relationship:HAS_EFFECT]->(object:Effect)
WHERE subject.name IN $entity_names
RETURN subject.name AS subject,
       labels(subject)[0] AS subject_label,
       type(relationship) AS relationship,
       object.name AS object,
       labels(object)[0] AS object_label,
       NULL AS source
LIMIT 10
""".strip()


def make_schema() -> GraphSchema:
    properties = {
        "Herb": [
            "name",
            "property_flavor",
            "meridian",
            "dosage",
            "taboo",
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
                for label in ("Herb", "Formula", "Disease", "Symptom", "Effect", "Source")
            ],
            "relationship_types": [
                {"type": "HAS_EFFECT"},
                {"type": "HAS_INGREDIENT"},
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
            ],
        }
    )


def make_plan(cypher: str = VALID_QUERY) -> CypherPlan:
    return CypherPlan(
        cypher=cypher,
        parameters={"entity_names": ["麻黄"]},
        used_entities=[UsedEntityRef(label="Herb", name="麻黄")],
        expected_result_shape="herb_effect_pairs",
    )


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def data(self):
        return self.rows

    def fetch(self, count):
        return self.rows[:count]


class FakeTransaction:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.committed = False
        self.rolled_back = False

    def run(self, query, parameters):
        self.queries.append((query, parameters))
        return FakeResult(self.rows)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class FakeSession:
    def __init__(self, index_rows=None, query_rows=None):
        self.index_rows = index_rows or []
        self.transaction = FakeTransaction(query_rows or [])
        self.session_run_calls = []
        self.begin_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def run(self, query, **parameters):
        self.session_run_calls.append((query, parameters))
        return FakeResult(self.index_rows)

    def begin_transaction(self, timeout):
        self.begin_calls.append(timeout)
        return self.transaction


class FakeDriver:
    def __init__(self, session):
        self.session_instance = session
        self.session_options = None
        self.closed = False

    def session(self, **options):
        self.session_options = options
        return self.session_instance

    def close(self):
        self.closed = True


def make_repository(session, database="neo4j"):
    schema = make_schema()
    guard = CypherGuard(schema, max_results=50)
    driver = FakeDriver(session)
    repository = Neo4jRepository(
        driver=driver,
        schema=schema,
        guard=guard,
        database=database,
        query_timeout_seconds=3,
        max_results=50,
    )
    return repository, driver


def test_load_entity_index_uses_read_session_and_supported_labels():
    session = FakeSession(
        index_rows=[
            {"label": "Herb", "name": "麻黄", "alias": "龙沙"},
            {"label": "Effect", "name": "发汗", "alias": None},
            {"label": "Unknown", "name": "忽略", "alias": None},
        ]
    )
    repository, driver = make_repository(session)

    index = repository.load_entity_index()

    assert len(index) == 2
    assert driver.session_options == {
        "default_access_mode": READ_ACCESS,
        "database": "neo4j",
    }
    assert session.session_run_calls[0][0] == ENTITY_INDEX_QUERY
    assert "Herb" in session.session_run_calls[0][1]["labels"]


def test_execute_read_uses_timeout_and_returns_json_rows():
    rows = [
        {
            "subject": "麻黄",
            "subject_label": "Herb",
            "relationship": "HAS_EFFECT",
            "object": "发汗",
            "object_label": "Effect",
            "source": None,
        }
    ]
    session = FakeSession(query_rows=rows)
    repository, _driver = make_repository(session)

    result = repository.execute_read(make_plan())

    assert result.row_count == 1
    assert result.rows == rows
    assert result.limit_applied == 10
    assert session.begin_calls == [3]
    assert session.transaction.committed is True
    assert session.transaction.rolled_back is False


def test_explain_uses_explain_prefix_without_writing():
    session = FakeSession(query_rows=[])
    repository, _driver = make_repository(session)

    repository.explain(make_plan())

    query, parameters = session.transaction.queries[0]
    assert query.startswith("EXPLAIN MATCH")
    assert parameters == {"entity_names": ["麻黄"]}


def test_repository_rejects_write_query_before_opening_transaction():
    session = FakeSession(query_rows=[])
    repository, _driver = make_repository(session)
    plan = make_plan(VALID_QUERY.replace("MATCH", "CREATE"))

    with pytest.raises(CypherGuardError):
        repository.execute_read(plan)

    assert session.begin_calls == []


def test_execute_attributes_uses_controlled_query_and_filters_fields():
    session = FakeSession(
        query_rows=[
            {
                "subject": "麻黄",
                "subject_label": "Herb",
                "properties": {
                    "name": "麻黄",
                    "property_flavor": "辛、微苦，温",
                    "meridian": "归肺经、膀胱经",
                    "taboo": "体虚自汗者忌服",
                },
                "sources": ["《中药大辞典》"],
            }
        ]
    )
    repository, _driver = make_repository(session)
    plan = AttributeQueryPlan(
        used_entities=[UsedEntityRef(label="Herb", name="麻黄")],
        selections=[
            AttributeSelection(
                label="Herb",
                attributes=["property_flavor", "meridian"],
            )
        ],
    )

    result = repository.execute_attributes(plan)

    assert result.row_count == 1
    assert result.rows == [
        {
            "subject": "麻黄",
            "subject_label": "Herb",
            "properties": {
                "meridian": "归肺经、膀胱经",
                "property_flavor": "辛、微苦，温",
            },
            "sources": ["《中药大辞典》"],
        }
    ]
    query, parameters = session.transaction.queries[0]
    assert "properties(subject)" in query
    assert parameters == {
        "labels": ["Herb"],
        "entity_names": ["麻黄"],
    }


def test_execute_attributes_rejects_non_whitelisted_field():
    session = FakeSession(query_rows=[])
    repository, _driver = make_repository(session)
    plan = AttributeQueryPlan(
        used_entities=[UsedEntityRef(label="Herb", name="麻黄")],
        selections=[
            AttributeSelection(
                label="Herb",
                attributes=["password"],
            )
        ],
    )

    with pytest.raises(RepositoryError, match="属性查询包含未允许的字段"):
        repository.execute_attributes(plan)

    assert session.begin_calls == []


def test_execute_aggregate_counts_distinct_related_entities():
    session = FakeSession(query_rows=[{"count": 4}])
    repository, _driver = make_repository(session)
    plan = AggregateQueryPlan(
        subject_label="Formula",
        relationship="HAS_INGREDIENT",
        object_label="Herb",
        used_entities=[UsedEntityRef(label="Formula", name="四君子汤")],
        direction="outgoing",
        description="四君子汤关联的药材",
    )

    result = repository.execute_aggregate(plan)

    assert result.rows == [{"count": 4}]
    query, parameters = session.transaction.queries[0]
    assert "count(DISTINCT object)" in query
    assert parameters == {"entity_names": ["四君子汤"]}


def test_execute_aggregate_rejects_wrong_anchor_direction():
    session = FakeSession(query_rows=[])
    repository, _driver = make_repository(session)
    plan = AggregateQueryPlan(
        subject_label="Formula",
        relationship="HAS_INGREDIENT",
        object_label="Herb",
        used_entities=[UsedEntityRef(label="Herb", name="人参")],
        direction="outgoing",
        description="人参关联的方剂",
    )

    with pytest.raises(RepositoryError, match="关系方向不匹配"):
        repository.execute_aggregate(plan)

    assert session.begin_calls == []


def test_execute_aggregate_requires_all_entities_for_intersection():
    session = FakeSession(query_rows=[{"count": 7}])
    repository, _driver = make_repository(session)
    plan = AggregateQueryPlan(
        subject_label="Formula",
        relationship="HAS_INGREDIENT",
        object_label="Herb",
        used_entities=[
            UsedEntityRef(label="Herb", name="人参"),
            UsedEntityRef(label="Herb", name="白术"),
        ],
        direction="incoming",
        require_all_entities=True,
        description="同时关联到人参、白术的方剂",
    )

    result = repository.execute_aggregate(plan)

    assert result.rows == [{"count": 7}]
    query, parameters = session.transaction.queries[0]
    assert query.count("EXISTS") == 2
    assert parameters == {
        "entity_0": "人参",
        "entity_1": "白术",
    }


def test_execute_multi_condition_list_returns_subject_conditions():
    rows = [
        {
            "subject": "四君子汤",
            "subject_label": "Formula",
            "conditions": [
                {
                    "relationship": "HAS_INGREDIENT",
                    "object": "人参",
                    "object_label": "Herb",
                    "sources": [],
                },
                {
                    "relationship": "HAS_INGREDIENT",
                    "object": "白术",
                    "object_label": "Herb",
                    "sources": ["《局方》"],
                },
            ],
        }
    ]
    session = FakeSession(query_rows=rows)
    repository, _driver = make_repository(session)
    plan = MultiConditionQueryPlan(
        subject_label="Formula",
        relationship="HAS_INGREDIENT",
        object_label="Herb",
        used_entities=[
            UsedEntityRef(label="Herb", name="人参"),
            UsedEntityRef(label="Herb", name="白术"),
        ],
        description="同时关联到人参、白术的方剂",
    )

    result = repository.execute_multi_condition_list(plan)

    assert result.rows == rows
    query, parameters = session.transaction.queries[0]
    assert query.count("EXISTS") == 2
    assert "collect(DISTINCT" in query
    assert parameters["entity_0"] == "人参"
    assert parameters["entity_1"] == "白术"
    assert parameters["entity_names"] == ["人参", "白术"]


def test_execute_multi_hop_returns_two_step_paths():
    rows = [
        {
            "root": "四君子汤",
            "root_label": "Formula",
            "first_relationship": "HAS_INGREDIENT",
            "intermediate": "人参",
            "intermediate_label": "Herb",
            "second_relationship": "HAS_EFFECT",
            "target": "补气",
            "target_label": "Effect",
            "sources": [],
        }
    ]
    session = FakeSession(query_rows=rows)
    repository, _driver = make_repository(session)
    plan = MultiHopQueryPlan(
        steps=[
            MultiHopStep(
                subject_label="Formula",
                relationship="HAS_INGREDIENT",
                object_label="Herb",
            ),
            MultiHopStep(
                subject_label="Herb",
                relationship="HAS_EFFECT",
                object_label="Effect",
            ),
        ],
        used_entities=[UsedEntityRef(label="Formula", name="四君子汤")],
        description="四君子汤所含药材的功效",
    )

    result = repository.execute_multi_hop(plan)

    assert result.rows == rows
    query, parameters = session.transaction.queries[0]
    assert "first:HAS_INGREDIENT" in query
    assert "second:HAS_EFFECT" in query
    assert parameters == {"entity_names": ["四君子汤"]}
