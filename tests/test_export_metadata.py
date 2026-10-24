# -*- coding: utf-8 -*-
"""graph_database.export_metadata 的单元测试，不连接真实 Neo4j。"""

import json

import pytest

from graph_database import export_metadata


class FakeResult:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.queries = []

    def run(self, query, **parameters):
        self.queries.append((query, parameters))
        if "MATCH (node) RETURN count(node) AS count" in query:
            return FakeResult([{"count": 3}])
        if "MATCH ()-[relationship]->() RETURN count(relationship) AS count" in query:
            return FakeResult([{"count": 2}])
        if "UNWIND labels(node) AS label" in query:
            return FakeResult(
                [
                    {"label": "Disease", "count": 1},
                    {"label": "Herb", "count": 2},
                ]
            )
        if "UNWIND keys(node) AS property" in query and "MATCH (node:`Herb`)" in query:
            return FakeResult(
                [
                    {"property": "name", "count": 2},
                    {"property": "property_flavor", "count": 1},
                ]
            )
        if "UNWIND keys(node) AS property" in query and "MATCH (node:`Disease`)" in query:
            return FakeResult([{"property": "name", "count": 1}])
        if "node[$property] AS value" in query and "`Herb`" in query:
            values = {
                "name": ["麻黄", "桂枝"],
                "property_flavor": ["辛"],
            }
            return FakeResult([{"value": value} for value in values[parameters["property"]]])
        if "node[$property] AS value" in query and "`Disease`" in query:
            return FakeResult([{"value": "感冒"}])
        if "UNWIND labels(source) AS source_label" in query:
            return FakeResult(
                [
                    {
                        "source_label": "Herb",
                        "relationship_type": "TREATS_DISEASE",
                        "target_label": "Disease",
                        "count": 2,
                    }
                ]
            )
        if "type(relationship) AS relationship_type" in query:
            return FakeResult([{"relationship_type": "TREATS_DISEASE", "count": 2}])
        if "UNWIND keys(relationship) AS property" in query:
            return FakeResult([])
        if "coalesce(source.name, elementId(source))" in query:
            return FakeResult(
                [
                    {"subject": "麻黄", "object": "感冒"},
                    {"subject": "桂枝", "object": "感冒"},
                ]
            )
        raise AssertionError(f"测试中没有匹配查询: {query}")


def test_collect_metadata_builds_labels_properties_and_triple_patterns():
    session = FakeSession()

    metadata = export_metadata.collect_metadata(
        session,
        database="neo4j",
        sample_size=2,
    )

    assert metadata["format_version"] == "1.1"
    assert metadata["database"]["name"] == "neo4j"
    assert metadata["database"]["node_count"] == 3
    assert metadata["database"]["relationship_count"] == 2
    assert "中医知识图谱" in metadata["database"]["description"]
    herb = next(item for item in metadata["node_labels"] if item["label"] == "Herb")
    assert herb["count"] == 2
    assert herb["identifier_property"] == "name"
    assert "药材实体" in herb["description"]
    flavor = next(item for item in herb["properties"] if item["name"] == "property_flavor")
    assert flavor["present_in_count"] == 1
    assert flavor["inferred_types"] == ["string"]
    assert flavor["sample_values"] == ["辛"]
    assert "性味" in flavor["description"]

    relationship = metadata["relationship_types"][0]
    assert relationship["type"] == "TREATS_DISEASE"
    assert relationship["count"] == 2
    assert relationship["properties"] == []
    assert "治疗" in relationship["description"]

    pattern = metadata["triple_patterns"][0]
    assert pattern["subject_label"] == "Herb"
    assert pattern["relationship_type"] == "TREATS_DISEASE"
    assert pattern["object_label"] == "Disease"
    assert pattern["count"] == 2
    assert pattern["examples"] == [
        {"subject": "麻黄", "object": "感冒"},
        {"subject": "桂枝", "object": "感冒"},
    ]
    assert "药材用于治疗" in pattern["description"]
    assert "REVERSE" not in json.dumps(metadata, ensure_ascii=False)


def test_build_markdown_contains_direction_and_llm_constraints():
    metadata = {
        "format_version": "1.0",
        "database": {"name": "neo4j", "node_count": 3, "relationship_count": 2},
        "node_labels": [
            {
                "label": "Herb",
                "count": 2,
                "identifier_property": "name",
                "properties": [
                    {
                        "name": "name",
                        "present_in_count": 2,
                        "inferred_types": ["string"],
                        "sample_values": ["麻黄", "桂枝"],
                    }
                ],
            }
        ],
        "relationship_types": [
            {"type": "TREATS_DISEASE", "count": 2, "properties": []}
        ],
        "triple_patterns": [
            {
                "subject_label": "Herb",
                "relationship_type": "TREATS_DISEASE",
                "object_label": "Disease",
                "count": 2,
                "examples": [{"subject": "麻黄", "object": "感冒"}],
            }
        ],
        "query_guidance": {
            "entity_lookup_property": "name",
            "rules": ["关系方向必须严格遵循 triple_patterns"],
        },
    }

    markdown = export_metadata.build_markdown(metadata)

    assert "`Herb`" in markdown
    assert "`TREATS_DISEASE`" in markdown
    assert "`(起点标签)-[:关系类型]->(终点标签)`" in markdown
    assert "麻黄 -> 感冒" in markdown
    assert "药材实体" in markdown
    assert "药材用于治疗某种疾病" in markdown
    assert "关系方向必须严格遵循 triple_patterns" in markdown


def test_write_metadata_files_creates_utf8_json_and_markdown(tmp_path):
    metadata = {
        "format_version": "1.0",
        "database": {"name": "default", "node_count": 0, "relationship_count": 0},
        "node_labels": [],
        "relationship_types": [],
        "triple_patterns": [],
        "query_guidance": {
            "entity_lookup_property": "name",
            "rules": ["只能使用实际存在的标签和关系类型"],
        },
    }

    json_path, markdown_path = export_metadata.write_metadata_files(metadata, tmp_path)

    written = json.loads(json_path.read_text(encoding="utf-8"))
    assert written["format_version"] == "1.1"
    assert "中医知识图谱" in written["database"]["description"]
    assert "中医知识图谱数据库元数据" in markdown_path.read_text(encoding="utf-8")
    assert "只能使用实际存在的标签和关系类型" in markdown_path.read_text(encoding="utf-8")


def test_enrich_metadata_adds_fallback_descriptions_for_unknown_items():
    metadata = {
        "format_version": "1.0",
        "database": {"name": "default", "node_count": 1, "relationship_count": 0},
        "node_labels": [
            {
                "label": "Custom",
                "count": 1,
                "properties": [{"name": "custom_key", "present_in_count": 1}],
            }
        ],
        "relationship_types": [{"type": "CUSTOM_REL", "count": 0, "properties": []}],
        "triple_patterns": [
            {
                "subject_label": "Custom",
                "relationship_type": "CUSTOM_REL",
                "object_label": "Custom",
                "count": 0,
                "examples": [],
            }
        ],
    }

    enriched = export_metadata.enrich_metadata(metadata)

    assert enriched is not metadata
    assert enriched["format_version"] == "1.1"
    assert "Custom 类型实体" in enriched["node_labels"][0]["description"]
    assert "custom_key 属性" in enriched["node_labels"][0]["properties"][0]["description"]
    assert "CUSTOM_REL 关系" in enriched["relationship_types"][0]["description"]
    assert "Custom 实体通过 CUSTOM_REL 关系" in enriched["triple_patterns"][0]["description"]


def test_collect_metadata_rejects_negative_sample_size():
    with pytest.raises(ValueError, match="sample_size"):
        export_metadata.collect_metadata(FakeSession(), sample_size=-1)


def test_quote_identifier_escapes_backticks():
    assert export_metadata.quote_identifier("Herb`Test") == "`Herb``Test`"
