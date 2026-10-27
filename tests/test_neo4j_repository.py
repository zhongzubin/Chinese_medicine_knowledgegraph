# -*- coding: utf-8 -*-
"""Neo4j 只读仓储测试，不连接真实数据库。"""

from neo4j import READ_ACCESS
import pytest

from graph_qa.cypher_guard import CypherGuard, CypherGuardError
from graph_qa.models import CypherPlan, UsedEntityRef
from graph_qa.neo4j_repository import ENTITY_INDEX_QUERY, Neo4jRepository
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
    return GraphSchema.from_mapping(
        {
            "format_version": "1.1",
            "node_labels": [
                {"label": label, "properties": [{"name": "name"}]}
                for label in ("Herb", "Formula", "Disease", "Symptom", "Effect", "Source")
            ],
            "relationship_types": [{"type": "HAS_EFFECT"}],
            "triple_patterns": [
                {
                    "subject_label": "Herb",
                    "relationship_type": "HAS_EFFECT",
                    "object_label": "Effect",
                }
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
