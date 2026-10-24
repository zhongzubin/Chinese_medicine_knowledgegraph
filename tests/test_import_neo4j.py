# -*- coding: utf-8 -*-
"""graph_database.import_data 的单元测试，不连接真实 Neo4j。"""

import json
from pathlib import Path

import pytest

from graph_database import import_data


def write_extract_file(path: Path, results):
    path.write_text(json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8")


class FakeResult:
    def consume(self):
        return None


class FakeTransaction:
    def __init__(self):
        self.queries = []

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        return FakeResult()


class FakeSession:
    def __init__(self):
        self.transaction = FakeTransaction()
        self.auto_commit_queries = []
        self.write_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def run(self, query, **parameters):
        self.auto_commit_queries.append((query, parameters))
        return FakeResult()

    def execute_write(self, function, *args):
        self.write_calls.append((function, args))
        return function(self.transaction, *args)


class FakeDriver:
    def __init__(self):
        self.session_instance = FakeSession()
        self.session_options = None

    def session(self, **options):
        self.session_options = options
        return self.session_instance


def test_load_graph_data_merges_entities_and_keeps_different_types(tmp_path):
    path = tmp_path / "extract.json"
    write_extract_file(
        path,
        [
            {
                "filename": "麻黄.txt",
                "extract_dict": {
                    "entities": [
                        {
                            "name": "麻黄",
                            "type": "Herb",
                            "attributes": {"property_flavor": "辛", "taboo": None},
                        },
                        {"name": "麻黄", "type": "Herb", "attributes": {"meridian": "归肺经"}},
                        {"name": "消渴", "type": "Disease"},
                        {"name": "消渴", "type": "Symptom"},
                    ],
                    "relations": [
                        {
                            "subject": "麻黄",
                            "subject_type": "Herb",
                            "relation": "TREATS_DISEASE",
                            "object": "消渴",
                            "object_type": "Disease",
                        }
                    ],
                },
            }
        ],
    )

    graph_data = import_data.load_graph_data([path])

    assert graph_data.record_count == 1
    assert graph_data.entity_occurrence_count == 4
    assert len(graph_data.entities) == 3
    assert graph_data.placeholder_entity_count == 0
    herb = next(
        entity
        for entity in graph_data.entities
        if entity.entity_type == "Herb" and entity.name == "麻黄"
    )
    assert herb.attributes == {"property_flavor": "辛", "meridian": "归肺经"}
    assert {(entity.entity_type, entity.name) for entity in graph_data.entities} >= {
        ("Disease", "消渴"),
        ("Symptom", "消渴"),
    }


def test_load_graph_data_adds_placeholders_for_missing_relation_endpoints(tmp_path):
    path = tmp_path / "extract.json"
    write_extract_file(
        path,
        [
            {
                "filename": "麻黄汤.txt",
                "extract_dict": {
                    "entities": [{"name": "麻黄汤", "type": "Formula"}],
                    "relations": [
                        {
                            "subject": "麻黄汤",
                            "subject_type": "Formula",
                            "relation": "FROM_SOURCE",
                            "object": "伤寒论",
                            "object_type": "Source",
                        }
                    ],
                },
            }
        ],
    )

    graph_data = import_data.load_graph_data([path])

    assert graph_data.placeholder_entity_count == 1
    assert any(
        entity.entity_type == "Source"
        and entity.name == "伤寒论"
        and entity.attributes == {}
        for entity in graph_data.entities
    )


def test_load_graph_data_deduplicates_relations_across_files(tmp_path):
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    relation = {
        "subject": "麻黄",
        "subject_type": "Herb",
        "relation": "HAS_EFFECT",
        "object": "发汗解表",
        "object_type": "Effect",
    }
    result = {
        "filename": "麻黄.txt",
        "extract_dict": {"entities": [], "relations": [relation]},
    }
    write_extract_file(first_path, [result])
    write_extract_file(second_path, [result])

    graph_data = import_data.load_graph_data([first_path, second_path])

    assert graph_data.record_count == 2
    assert graph_data.relation_occurrence_count == 2
    assert len(graph_data.relations) == 1


def test_load_graph_data_rejects_unknown_entity_type(tmp_path):
    path = tmp_path / "extract.json"
    write_extract_file(
        path,
        [
            {
                "filename": "invalid.txt",
                "extract_dict": {
                    "entities": [{"name": "未知实体", "type": "Unknown"}],
                    "relations": [],
                },
            }
        ],
    )

    with pytest.raises(import_data.GraphDataError, match="不支持的实体类型"):
        import_data.load_graph_data([path])


def test_import_graph_writes_constraints_nodes_and_relations_in_batches():
    entities = [
        import_data.EntityRecord("Disease", "感冒", {}),
        import_data.EntityRecord("Herb", "麻黄", {"property_flavor": "辛"}),
        import_data.EntityRecord("Herb", "桂枝", {}),
    ]
    relations = [
        import_data.RelationRecord("麻黄", "Herb", "TREATS_DISEASE", "感冒", "Disease"),
        import_data.RelationRecord("桂枝", "Herb", "TREATS_DISEASE", "感冒", "Disease"),
    ]
    graph_data = import_data.GraphData(entities, relations, 1, 3, 2, 0)
    driver = FakeDriver()

    import_data.import_graph(graph_data, driver, database="neo4j", batch_size=1)

    session = driver.session_instance
    assert driver.session_options == {"database": "neo4j"}
    assert len(session.auto_commit_queries) == len(import_data.ENTITY_TYPES)
    assert all("CREATE CONSTRAINT" in query for query, _ in session.auto_commit_queries)
    assert len(session.write_calls) == 5

    queries = [query for query, _ in session.transaction.queries]
    assert any("MERGE (node:`Herb`" in query for query in queries)
    assert any("MERGE (subject)-[:`TREATS_DISEASE`]->(object)" in query for query in queries)
    assert all("MATCH (subject:`Herb`" in query for query in queries if "TREATS_DISEASE" in query)


def test_import_graph_rejects_invalid_batch_size():
    graph_data = import_data.GraphData([], [], 0, 0, 0, 0)

    with pytest.raises(ValueError, match="batch_size"):
        import_data.import_graph(graph_data, FakeDriver(), batch_size=0)
